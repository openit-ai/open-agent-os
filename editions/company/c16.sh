#!/usr/bin/env bash
# Sourced by install.sh. C16 uses the C02 E host and C05 credential boundary.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/c02.sh
. "$repo_root/editions/company/c02.sh"
# shellcheck source=editions/company/c02-verify.sh
. "$repo_root/editions/company/c02-verify.sh"

company_c16_identifier() { [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; }
company_c16_task() { [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; }

company_c16_sql() {
  local org=$1 admin=$2 statement=$3 output line last=''
  output=$(psql -X -q -A -t -v ON_ERROR_STOP=1 \
    -c "SELECT set_config('company.org_id', '$org', false);" \
    -c "SELECT set_config('company.member_id', '$admin', false);" \
    -c "$statement" 2>/dev/null) || return 1
  while IFS= read -r line; do [[ -n $line ]] && last=$line; done <<< "$output"
  printf '%s' "$last"
}

company_c16_admin_context() {
  local rows row org='' admin='' first_org=''
  rows=$(run_root env -u PGOPTIONS -u PGHOSTADDR -u PGHOST -u PGUSER \
    -u PGSERVICE -u PGPASSFILE -u PGPASSWORD \
    runuser -u postgres -- psql --host="${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}" \
    --port="${PGPORT:-5432}" --dbname="$PGDATABASE" -X -q -A -t \
    -c "SELECT m.org_id::text || '|' || m.id::text
          FROM company.members m
          JOIN company.organizations o ON o.id = m.org_id
         WHERE m.role = 'admin' AND m.state = 'active' AND o.state = 'active'
         ORDER BY o.created_at, m.created_at, m.id LIMIT 2" 2>/dev/null) || return 1
  while IFS= read -r row; do
    [[ -n $row ]] || continue
    org=${row%%|*}
    admin=${row#*|}
    [[ -z $first_org ]] && first_org=$org
    [[ $org != "$first_org" ]] && return 1
  done <<< "$rows"
  [[ $first_org =~ ^[0-9a-f-]{36}$ && $admin =~ ^[0-9a-f-]{36}$ ]] || return 1
  printf '%s %s\n' "$org" "$admin"
}

company_c16_current_state() {
  local org=$1 value=''
  value=$(run_root env -u PGOPTIONS -u PGHOSTADDR -u PGHOST -u PGUSER \
    -u PGSERVICE -u PGPASSFILE -u PGPASSWORD \
    runuser -u postgres -- psql --host="${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}" \
    --port="${PGPORT:-5432}" --dbname="$PGDATABASE" -X -q -A -t \
    -c "SELECT g13_state FROM company.llm_settings WHERE org_id = '$org'::uuid" 2>/dev/null) || return 1
  printf '%s' "${value%%$'\n'*}"
}

company_c16_current_default() {
  local org=$1 value=''
  value=$(run_root env -u PGOPTIONS -u PGHOSTADDR -u PGHOST -u PGUSER \
    -u PGSERVICE -u PGPASSFILE -u PGPASSWORD \
    runuser -u postgres -- psql --host="${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}" \
    --port="${PGPORT:-5432}" --dbname="$PGDATABASE" -X -q -A -t \
    -c "SELECT m.provider || '|' || m.model_name
          FROM company.llm_models m
          JOIN company.llm_routes r
            ON r.org_id = m.org_id AND r.primary_model_id = m.id
         WHERE r.org_id = '$org'::uuid AND r.task = 'default'
           AND m.tier = 'standard' AND m.state = 'active'
         LIMIT 1" 2>/dev/null) || return 1
  printf '%s' "${value%%$'\n'*}"
}

company_c16_key_name() {
  printf '%s_API_KEY' "$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')"
}

company_c16_env_has_key() {
  local file=$1 name=$2
  grep -Eq "^${name}=.+" "$file" 2>/dev/null
}

company_c16_write_key() {
  local file=$1 name=$2
  printf '%s' "${OAOS_COMPANY_G13_API_KEY:-}" |
    python3 "$repo_root/editions/company/env-key.py" "$file" "$name"
}

company_c16_configure() {
  local org=$1 admin=$2 default_provider=$3 default_model=$4 state=$5
  local decision model_id premium_provider='' premium_model='' billing='flat' cap=0 approved=false
  local premium_decision premium_id task route_decision key_name apply_state=${OAOS_COMPANY_G13_STATE:+1}
  local tasks task_list

  decision=$(company_c16_sql "$org" "$admin" \
    "SELECT decision || '|' || model_id FROM company.configure_llm_model('$default_provider', '$default_model', 'standard', 'flat', 0, false)") || return 1
  [[ $decision == configured\|* ]] || { warn 'C16 BLOCKED: default model configuration was rejected.'; return 3; }
  model_id=${decision#*|}
  decision=$(company_c16_sql "$org" "$admin" \
    "SELECT decision FROM company.set_llm_route('default', '$model_id', NULL)") || return 1
  [[ $decision == configured ]] || { warn 'C16 BLOCKED: default route configuration was rejected.'; return 3; }

  if [[ $state == selected && -z $apply_state ]]; then
    info 'C16 preserved the existing G13 selected configuration.'
    return 0
  fi
  if [[ $state != selected ]]; then
    decision=$(company_c16_sql "$org" "$admin" \
      "SELECT company.set_llm_g13_state('skip', 'not_selected')") || return 1
    [[ $decision == configured ]] || { warn 'C16 BLOCKED: G13 SKIP state was rejected.'; return 3; }
    info 'C16 G13 SKIP(reason=not_selected); default model only.'
    return 0
  fi

  premium_provider=${OAOS_COMPANY_G13_PROVIDER:-}
  premium_model=${OAOS_COMPANY_G13_MODEL:-}
  billing=${OAOS_COMPANY_G13_BILLING_MODE:-flat}
  cap=${OAOS_COMPANY_G13_MONTHLY_CAP_USD:-0}
  [[ ${OAOS_COMPANY_G13_BILLING_APPROVED:-no} == yes ]] && approved=true
  company_c16_identifier "$premium_provider" && company_c16_identifier "$premium_model" || {
    warn 'C16 BLOCKED: Set OAOS_COMPANY_G13_PROVIDER and OAOS_COMPANY_G13_MODEL.'; return 3;
  }
  [[ $billing == flat || $billing == usage ]] || {
    warn 'C16 BLOCKED: G13 billing mode must be flat or usage.'; return 3;
  }
  [[ $billing != usage || $cap =~ ^[0-9]+([.][0-9]{1,2})?$ ]] || {
    warn 'C16 BLOCKED: usage billing requires a positive monthly cap.'; return 3;
  }
  if [[ $billing == usage ]]; then
    [[ $approved == true ]] || {
      warn 'C16 BLOCKED: usage billing path requires approval.'; return 3;
    }
    [[ $cap =~ ^[1-9][0-9]*([.][0-9]{1,2})?$ || $cap =~ ^0[.][0-9]*[1-9][0-9]*$ ]] || {
      warn 'C16 BLOCKED: usage billing requires a positive monthly cap.'; return 3;
    }
  else
    cap=0
  fi
  key_name=$(company_c16_key_name "$premium_provider")
  [[ ${key_name} =~ ^[A-Z0-9_]+$ ]] || { warn 'C16 BLOCKED: provider key name is invalid.'; return 3; }
  if [[ -z ${OAOS_COMPANY_G13_API_KEY:-} ]] && ! company_c16_env_has_key "$HOME/.config/oaos-company/.env" "$key_name"; then
    warn 'C16 BLOCKED: G13 selected requires the provider API key in the Company EnvironmentFile.'
    return 3
  fi

  decision=$(company_c16_sql "$org" "$admin" \
    "SELECT company.set_llm_g13_state('selected', 'operator_approved')") || return 1
  [[ $decision == configured ]] || { warn 'C16 BLOCKED: G13 selected state was rejected.'; return 3; }
  premium_decision=$(company_c16_sql "$org" "$admin" \
    "SELECT decision || '|' || model_id FROM company.configure_llm_model('$premium_provider', '$premium_model', 'premium', '$billing', $cap, $approved)") || return 1
  [[ $premium_decision == configured\|* ]] || {
    warn 'C16 BLOCKED: premium model configuration was rejected.'; return 3;
  }
  premium_id=${premium_decision#*|}
  tasks=${OAOS_COMPANY_G13_TASKS:-premium}
  IFS=',' read -r -a task_list <<< "$tasks"
  for task in "${task_list[@]}"; do
    company_c16_task "$task" || { warn 'C16 BLOCKED: G13 task name is invalid.'; return 3; }
    route_decision=$(company_c16_sql "$org" "$admin" \
      "SELECT decision FROM company.set_llm_route('$task', '$premium_id', '$model_id')") || return 1
    [[ $route_decision == configured ]] || {
      warn "C16 BLOCKED: route $task was rejected."; return 3;
    }
  done
  if [[ -n ${OAOS_COMPANY_G13_API_KEY:-} ]]; then
    company_c16_write_key "$HOME/.config/oaos-company/.env" "$key_name" || {
      warn 'C16 BLOCKED: cannot store the G13 provider key safely.'; return 3;
    }
  fi
  chmod 600 "$HOME/.config/oaos-company/.env" || return 1
  info 'C16 G13 selected: premium routes configured with standard fallback; key stored in Company EnvironmentFile.'
}

company_c16_apply() {
  local ready role_safe check_code=0 socket_dir snapshot org='' admin='' current_state=''
  local default_provider default_model current_default state
  [[ $(stage_status c01) == verified && $(stage_status c02) == verified && \
     $(stage_status c03) == verified && $(stage_status c04) == verified && \
     $(stage_status c05) == verified && $(stage_status c07) == verified ]] || {
    warn 'C16 BLOCKED: C01, C02, C03, C04, C05 and C07 must be verified on E.'; return 3;
  }
  company_c02_load_settings || { warn 'C16 BLOCKED: C02 E deployment settings are invalid.'; return 3; }
  company_c02_preflight || return 3
  export PGHOSTADDR=127.0.0.1
  if ! have_cmd psql || ! have_cmd pg_dump || ! have_cmd sha256sum || ! have_cmd python3; then
    warn 'C16 BLOCKED: PostgreSQL tools and Python are required.'; return 3
  fi
  export PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  ready=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' 2>/dev/null) || {
    warn 'C16 BLOCKED: E database connection failed.'; return 3;
  }
  [[ $ready == 1 ]] || { warn 'C16 BLOCKED: E database did not answer.'; return 3; }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'C16 BLOCKED: database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'C16 BLOCKED: database role bypasses RLS.'; return 3; }

  read -r org admin < <(company_c16_admin_context) || {
    warn 'C16 BLOCKED: a single organization with an active G10 administrator is required.'; return 3;
  }
  [[ -n $org && -n $admin ]] || {
    warn 'C16 BLOCKED: a single organization with an active G10 administrator is required.'; return 3;
  }

  bash "$repo_root/editions/company/migrate.sh" --check --through 5 >/dev/null 2>&1 || check_code=$?
  case $check_code in
    0) ;;
    3)
      if ! have_cmd psql || ! have_cmd pg_dump; then return 3; fi
      snapshot=$(mktemp -d "$OAOS_COMPANY_ROLLBACK_DIR/c16-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX") || return 1
      chmod 700 "$snapshot"
      socket_dir=${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}
      if ! company_c02_valid_path "$socket_dir" || [[ ! -d $socket_dir ]] ||
         [[ ! ${PGPORT:-5432} =~ ^[0-9]{1,5}$ ]]; then
        warn 'C16 BLOCKED: local PostgreSQL backup socket or port is invalid.'; return 3
      fi
      if ! (umask 077; run_root runuser -u postgres -- env -u PGOPTIONS -u PGHOSTADDR \
          -u PGHOST -u PGUSER -u PGSERVICE -u PGPASSFILE -u PGPASSWORD \
          pg_dump --host="$socket_dir" --port="${PGPORT:-5432}" \
          --dbname="$PGDATABASE" --format=custom > "$snapshot/company.dump" 2>/dev/null) ||
          [[ ! -s $snapshot/company.dump ]]; then
        warn 'C16 BLOCKED: E database backup failed or is empty.'; return 3
      fi
      chmod 600 "$snapshot/company.dump"
      bash "$repo_root/editions/company/migrate.sh" --through 5 || return 1 ;;
    *) warn 'C16 migration history or checksum is invalid.'; return 1 ;;
  esac

  current_state=$(company_c16_current_state "$org") || current_state=''
  state=${OAOS_COMPANY_G13_STATE:-$current_state}
  [[ -n $state ]] || state=skip
  [[ $state == skip || $state == selected ]] || {
    warn 'C16 BLOCKED: OAOS_COMPANY_G13_STATE must be skip or selected.'; return 3;
  }

  default_provider=${OAOS_COMPANY_DEFAULT_PROVIDER:-}
  default_model=${OAOS_COMPANY_DEFAULT_MODEL:-}
  if [[ -z $default_provider || -z $default_model ]]; then
    current_default=$(company_c16_current_default "$org") || current_default=''
    default_provider=${current_default%%|*}
    default_model=${current_default#*|}
  fi
  if ! company_c16_identifier "$default_provider" || ! company_c16_identifier "$default_model"; then
    warn 'C16 BLOCKED: set OAOS_COMPANY_DEFAULT_PROVIDER and OAOS_COMPANY_DEFAULT_MODEL from G1.'
    return 3
  fi

  company_c16_configure "$org" "$admin" "$default_provider" "$default_model" "$state" || return $?
  info 'C16 routing contract applied; run the E read-back.'
}
