#!/usr/bin/env bash
# Sourced by company-verify.sh. Uses only E loopback PostgreSQL.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/c16.sh
. "$repo_root/editions/company/c16.sh"

company_c16_verify() {
  local role_safe result=0 org='' admin='' state='' key_name='' env_file
  [[ $(stage_status c01) == verified && $(stage_status c02) == verified && \
     $(stage_status c03) == verified && $(stage_status c04) == verified && \
     $(stage_status c05) == verified && $(stage_status c07) == verified ]] || {
    warn 'C16 read-back needs C01, C02, C03, C04, C05 and C07 verified on E.'; return 3;
  }
  [[ $(stage_status c16) == applied || $(stage_status c16) == verified ]] || {
    warn 'C16 has not been applied.'; return 3;
  }
  company_c02_load_settings || { warn 'C02 E deployment settings are invalid.'; return 3; }
  [[ $PGHOST == 127.0.0.1 || $PGHOST == localhost ]] || {
    warn 'C16 read-back requires loopback PostgreSQL.'; return 3;
  }
  export PGHOSTADDR=127.0.0.1 PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  have_cmd psql || { warn 'psql is required for C16 read-back.'; return 3; }
  psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1 || {
    warn 'E database connection failed.'; return 3;
  }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'C16 database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'C16 read-back needs a non-BYPASSRLS role.'; return 3; }
  bash "$repo_root/editions/company/migrate.sh" --check --through 5 >/dev/null 2>&1 || result=$?
  if ((result)); then
    if ((result == 3)); then warn 'C16 migration is pending.'; return 3; fi
    warn 'C16 migration checksum or version read-back failed.'; return 1
  fi
  read -r org admin < <(company_c16_admin_context) || true
  [[ -n $org && -n $admin ]] || {
    warn 'C16 read-back needs a single organization with an active administrator.'; return 3;
  }
  state=$(company_c16_current_state "$org") || state=''
  env_file="$HOME/.config/oaos-company/.env"
  if [[ $state == selected ]]; then
    key_name=$(run_root env -u PGOPTIONS -u PGHOSTADDR -u PGHOST -u PGUSER \
      -u PGSERVICE -u PGPASSFILE -u PGPASSWORD \
      runuser -u postgres -- psql --host="${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}" \
      --port="${PGPORT:-5432}" --dbname="$PGDATABASE" -X -q -A -t \
      -c "SELECT provider FROM company.llm_models
           WHERE org_id = '$org'::uuid AND tier = 'premium' AND state = 'active'
           ORDER BY created_at LIMIT 1" 2>/dev/null) || key_name=''
    key_name=$(company_c16_key_name "${key_name%%$'\n'*}")
    if [[ ! $key_name =~ ^[A-Z0-9_]+$ ]] || ! company_c16_env_has_key "$env_file" "$key_name"; then
      printf '%-28s FAIL\n' 'G13 provider key presence'
      printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
      return 1
    fi
  fi
  [[ $(stat -c %a "$env_file") == 600 ]] || {
    printf '%-28s FAIL\n' 'EnvironmentFile permissions'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  }
  if ! psql -X -q -v ON_ERROR_STOP=1 -v org="$org" -v admin="$admin" \
      -f "$repo_root/editions/company/c16-readback.sql" >/dev/null 2>&1; then
    printf '%-28s FAIL\n' 'LLM routing response queries'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  printf '%-28s PASS\n' 'Migration SHA-256/version 5'
  printf '%-28s PASS\n' 'Default route and G13 state'
  printf '%-28s PASS\n' 'Billing approval boundary'
  printf '%-28s PASS\n' 'Monthly cap enforcement'
  printf '%-28s PASS\n' 'Task premium routing'
  printf '%-28s PASS\n' 'Standard fallback on cap/SKIP'
  printf '%-28s PASS\n' 'Owner and tenant isolation'
  printf '%-28s PASS\n' 'Audit trail and fixture rollback'
  printf 'Summary: PASS=8 FAIL=0 MANUAL=0 SKIP=0\n'
  stage_mark c16 verified "C16 E LLM routing SQL responses passed (G13 ${state:-skip}); fixture rolled back"
}
