#!/usr/bin/env bash
# Shared C11-C15 logic. Sourced by c11.sh..c15.sh. No credential is echoed.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/c02.sh
. "$repo_root/editions/company/c02.sh"
# shellcheck source=editions/company/c02-verify.sh
. "$repo_root/editions/company/c02-verify.sh"

company_adapters_prefix() {
  case $1 in
    slack) printf 'SLACK' ;;
    notion) printf 'NOTION' ;;
    google_workspace) printf 'GWS' ;;
    microsoft_365) printf 'M365' ;;
    *) return 1 ;;
  esac
}

company_adapters_stage() {
  case $1 in
    c11) printf 'slack' ;;
    c12) printf 'notion' ;;
    c13) printf 'google_workspace' ;;
    c14) printf 'microsoft_365' ;;
    *) return 1 ;;
  esac
}

company_adapters_config_var() {
  case $1 in
    slack) printf 'OAOS_COMPANY_SLACK_STATE OAOS_COMPANY_SLACK_WORKSPACE_ID OAOS_COMPANY_SLACK_BOT_TOKEN OAOS_COMPANY_SLACK_SIGNING_SECRET' ;;
    notion) printf 'OAOS_COMPANY_NOTION_STATE OAOS_COMPANY_NOTION_WORKSPACE_ID OAOS_COMPANY_NOTION_TOKEN' ;;
    google_workspace) printf 'OAOS_COMPANY_GWS_STATE OAOS_COMPANY_GWS_WORKSPACE_ID OAOS_COMPANY_GWS_CLIENT_ID OAOS_COMPANY_GWS_CLIENT_SECRET OAOS_COMPANY_GWS_REDIRECT_URI' ;;
    microsoft_365) printf 'OAOS_COMPANY_M365_STATE OAOS_COMPANY_M365_WORKSPACE_ID OAOS_COMPANY_M365_TENANT_ID OAOS_COMPANY_M365_CLIENT_ID OAOS_COMPANY_M365_CLIENT_SECRET OAOS_COMPANY_M365_REDIRECT_URI' ;;
    *) return 1 ;;
  esac
}

company_adapters_scopes() {
  case $1 in
    google_workspace) printf '%s\n' 'https://www.googleapis.com/auth/gmail.readonly' 'https://www.googleapis.com/auth/drive.readonly' 'https://www.googleapis.com/auth/calendar.readonly' ;;
    microsoft_365) printf '%s\n' 'Mail.Read' 'Files.Read' 'Calendars.Read' ;;
    *) : ;;
  esac
}

company_adapters_map_files() {
  case $1 in
    slack) printf 'SLACK_BOT_TOKEN:OAOS_COMPANY_SLACK_BOT_TOKEN SLACK_SIGNING_SECRET:OAOS_COMPANY_SLACK_SIGNING_SECRET' ;;
    notion) printf 'NOTION_TOKEN:OAOS_COMPANY_NOTION_TOKEN' ;;
    google_workspace) printf 'GOOGLE_WORKSPACE_CLIENT_ID:OAOS_COMPANY_GWS_CLIENT_ID GOOGLE_WORKSPACE_CLIENT_SECRET:OAOS_COMPANY_GWS_CLIENT_SECRET' ;;
    microsoft_365) printf 'MICROSOFT_365_CLIENT_ID:OAOS_COMPANY_M365_CLIENT_ID MICROSOFT_365_CLIENT_SECRET:OAOS_COMPANY_M365_CLIENT_SECRET' ;;
    *) return 1 ;;
  esac
}

company_adapters_env_has_key() { grep -Eq "^${2}=.+" "$1" 2>/dev/null; }

company_adapters_write_key() {
  printf '%s' "$3" | python3 "$repo_root/editions/company/env-key.py" "$1" "$2"
}

company_adapters_admin_context() {
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
  printf '%s %s\n' "$first_org" "$admin"
}

company_adapters_sql() {
  local org=$1 admin=$2 statement=$3 output line last=''
  output=$(psql -X -q -A -t -v ON_ERROR_STOP=1 \
    -c "SELECT set_config('company.org_id', '$org', false);" \
    -c "SELECT set_config('company.member_id', '$admin', false);" \
    -c "$statement" 2>/dev/null) || return 1
  while IFS= read -r line; do [[ -n $line ]] && last=$line; done <<< "$output"
  printf '%s' "$last"
}

company_adapters_current_state() {
  local org=$1 provider=$2 value=''
  value=$(run_root env -u PGOPTIONS -u PGHOSTADDR -u PGHOST -u PGUSER \
    -u PGSERVICE -u PGPASSFILE -u PGPASSWORD \
    runuser -u postgres -- psql --host="${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}" \
    --port="${PGPORT:-5432}" --dbname="$PGDATABASE" -X -q -A -t \
    -c "SELECT state FROM company.integration_settings
          WHERE org_id = '$org'::uuid AND provider = '$provider'" 2>/dev/null) || return 1
  printf '%s' "${value%%$'\n'*}"
}

company_adapters_quote_list() {
  local out='' item
  for item in "$@"; do
    out+="${out:+,}'$item'"
  done
  printf 'ARRAY[%s]' "$out"
}

company_adapters_preflight() {
  local provider=$1 ready role_safe
  [[ $(stage_status c01) == verified && $(stage_status c02) == verified && \
     $(stage_status c03) == verified && $(stage_status c04) == verified && \
     $(stage_status c05) == verified ]] || {
    warn 'Integration BLOCKED: C01, C02, C03, C04 and C05 must be verified on E.'; return 3;
  }
  company_c02_load_settings || { warn 'Integration BLOCKED: C02 E deployment settings are invalid.'; return 3; }
  company_c02_preflight || return 3
  export PGHOSTADDR=127.0.0.1
  if ! have_cmd psql || ! have_cmd pg_dump || ! have_cmd sha256sum || ! have_cmd python3; then
    warn 'Integration BLOCKED: PostgreSQL tools and Python are required.'; return 3
  fi
  export PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  ready=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' 2>/dev/null) || {
    warn 'Integration BLOCKED: E database connection failed.'; return 3;
  }
  [[ $ready == 1 ]] || { warn 'Integration BLOCKED: E database did not answer.'; return 3; }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'Integration BLOCKED: database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'Integration BLOCKED: database role bypasses RLS.'; return 3; }
  [[ $provider =~ ^(slack|notion|google_workspace|microsoft_365)$ ]] || {
    warn 'Integration BLOCKED: unknown provider.'; return 3;
  }
}

company_adapters_migrate() {
  local check_code=0 snapshot socket_dir
  bash "$repo_root/editions/company/migrate.sh" --check --through 6 >/dev/null 2>&1 || check_code=$?
  case $check_code in
    0) return 0 ;;
    3) ;;
    *) warn 'Integration migration history or checksum is invalid.'; return 1 ;;
  esac
  snapshot=$(mktemp -d "$OAOS_COMPANY_ROLLBACK_DIR/adapters-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX") || return 1
  chmod 700 "$snapshot"
  socket_dir=${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}
  if ! company_c02_valid_path "$socket_dir" || [[ ! -d $socket_dir ]] ||
     [[ ! ${PGPORT:-5432} =~ ^[0-9]{1,5}$ ]]; then
    warn 'Integration BLOCKED: local PostgreSQL backup socket or port is invalid.'; return 3
  fi
  if ! (umask 077; run_root runuser -u postgres -- env -u PGOPTIONS -u PGHOSTADDR \
      -u PGHOST -u PGUSER -u PGSERVICE -u PGPASSFILE -u PGPASSWORD \
      pg_dump --host="$socket_dir" --port="${PGPORT:-5432}" \
      --dbname="$PGDATABASE" --format=custom > "$snapshot/company.dump" 2>/dev/null) ||
      [[ ! -s $snapshot/company.dump ]]; then
    warn 'Integration BLOCKED: E database backup failed or is empty.'; return 3
  fi
  chmod 600 "$snapshot/company.dump"
  bash "$repo_root/editions/company/migrate.sh" --through 6 || return 1
}

company_adapters_store_keys() {
  local provider=$1 mapping entry name source value env_file="$HOME/.config/oaos-company/.env"
  mapping=$(company_adapters_map_files "$provider")
  for entry in $mapping; do
    name=${entry%%:*}
    source=${entry#*:}
    value=${!source:-}
    if [[ -n $value ]]; then
      company_adapters_write_key "$env_file" "$name" "$value" || return 1
    fi
    company_adapters_env_has_key "$env_file" "$name" || return 3
  done
  chmod 600 "$env_file" || return 1
}

company_adapters_apply() {
  local provider=$1 prefix state_var workspace_var workspace redirect tenant scopes_sql
  local org='' admin='' current_state state decision sql_workspace sql_tenant sql_redirect
  local scope_lines=() scope
  prefix=$(company_adapters_prefix "$provider") || { warn 'Integration BLOCKED: unknown provider.'; return 3; }
  state_var="OAOS_COMPANY_${prefix}_STATE"
  workspace_var="OAOS_COMPANY_${prefix}_WORKSPACE_ID"

  company_adapters_preflight "$provider" || return $?
  read -r org admin < <(company_adapters_admin_context) || true
  [[ -n $org && -n $admin ]] || {
    warn 'Integration BLOCKED: a single organization with an active G10 administrator is required.'; return 3;
  }

  company_adapters_migrate || return $?

  current_state=$(company_adapters_current_state "$org" "$provider") || current_state=''
  state=${!state_var:-$current_state}
  [[ -n $state ]] || state=skip
  [[ $state == skip || $state == selected ]] || {
    warn "Integration BLOCKED: ${state_var} must be skip or selected."; return 3;
  }

  if [[ $state == skip ]]; then
    decision=$(company_adapters_sql "$org" "$admin" \
      "SELECT company.set_integration_state('$provider', 'skip', 'not_selected')") || return 1
    [[ $decision == configured ]] || { warn 'Integration BLOCKED: SKIP state was rejected.'; return 3; }
    info "Integration $provider SKIP(reason=not_selected); no credential or service call."
    return 0
  fi

  workspace=${!workspace_var:-}
  [[ $workspace =~ ^[A-Za-z0-9._:@-]{1,120}$ ]] || {
    warn "Integration BLOCKED: set $workspace_var for the selected provider."; return 3;
  }
  sql_workspace=$workspace
  sql_tenant=NULL
  sql_redirect=NULL
  if [[ $provider == google_workspace || $provider == microsoft_365 ]]; then
    redirect=$(company_adapters_redirect_var "$provider")
    [[ $redirect =~ ^https://[A-Za-z0-9./_-]+$ ]] || {
      warn 'Integration BLOCKED: a valid HTTPS OAuth redirect URI is required.'; return 3;
    }
    sql_redirect="'$redirect'"
    while IFS= read -r scope; do [[ -n $scope ]] && scope_lines+=("$scope"); done < <(company_adapters_scopes "$provider")
    ((${#scope_lines[@]})) || { warn 'Integration BLOCKED: read-only scopes are required.'; return 3; }
    scopes_sql=$(company_adapters_quote_list "${scope_lines[@]}")
    if [[ $provider == microsoft_365 ]]; then
      tenant=${OAOS_COMPANY_M365_TENANT_ID:-}
      [[ $tenant =~ ^[A-Za-z0-9._:-]{1,120}$ ]] || {
        warn 'Integration BLOCKED: OAOS_COMPANY_M365_TENANT_ID is required.'; return 3;
      }
      sql_tenant="'$tenant'"
    fi
  else
    scopes_sql='NULL'
  fi

  decision=$(company_adapters_sql "$org" "$admin" \
    "SELECT company.set_integration_state('$provider', 'selected', 'operator_selected')") || return 1
  if [[ $decision == conflict ]]; then
    warn 'Integration BLOCKED: the productivity suite is already chosen; Google Workspace and Microsoft 365 are mutually exclusive.'
    return 3
  fi
  [[ $decision == configured ]] || { warn 'Integration BLOCKED: selection was rejected.'; return 3; }

  decision=$(company_adapters_sql "$org" "$admin" \
    "SELECT r.decision FROM company.configure_integration_app('$provider', '$sql_workspace', $sql_tenant, $sql_redirect, $scopes_sql) r") || return 1
  [[ $decision == configured ]] || {
    warn "Integration BLOCKED: organization app configuration was rejected ($decision)."; return 3;
  }

  company_adapters_store_keys "$provider" || {
    warn 'Integration BLOCKED: organization app credentials are missing from the Company EnvironmentFile.'; return 3;
  }
  info "Integration $provider selected; organization app configured and credentials stored in the Company EnvironmentFile."
}

company_adapters_redirect_var() {
  case $1 in
    google_workspace) printf '%s' "${OAOS_COMPANY_GWS_REDIRECT_URI:-}" ;;
    microsoft_365) printf '%s' "${OAOS_COMPANY_M365_REDIRECT_URI:-}" ;;
    *) printf '' ;;
  esac
}
