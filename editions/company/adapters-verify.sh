#!/usr/bin/env bash
# Sourced by company-verify.sh. Uses only E loopback PostgreSQL.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/adapters.sh
. "$repo_root/editions/company/adapters.sh"

company_adapters_verify() {
  local provider=$1 stage=$2 role_safe result=0 org='' admin='' state='' mapping entry name
  [[ $(stage_status c01) == verified && $(stage_status c02) == verified && \
     $(stage_status c03) == verified && $(stage_status c04) == verified && \
     $(stage_status c05) == verified ]] || {
    warn 'Integration read-back needs C01, C02, C03, C04 and C05 verified on E.'; return 3;
  }
  [[ $(stage_status "$stage") == applied || $(stage_status "$stage") == verified || \
     $(stage_status "$stage") == SKIP ]] || {
    warn "Integration read-back: $stage has not been applied."; return 3;
  }
  company_c02_load_settings || { warn 'C02 E deployment settings are invalid.'; return 3; }
  [[ $PGHOST == 127.0.0.1 || $PGHOST == localhost ]] || {
    warn 'Integration read-back requires loopback PostgreSQL.'; return 3;
  }
  export PGHOSTADDR=127.0.0.1 PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  have_cmd psql || { warn 'psql is required for the integration read-back.'; return 3; }
  psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1 || {
    warn 'E database connection failed.'; return 3;
  }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'Integration database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'Integration read-back needs a non-BYPASSRLS role.'; return 3; }
  bash "$repo_root/editions/company/migrate.sh" --check --through 6 >/dev/null 2>&1 || result=$?
  if ((result)); then
    if ((result == 3)); then warn 'Integration migration is pending.'; return 3; fi
    warn 'Integration migration checksum or version read-back failed.'; return 1
  fi
  read -r org admin < <(company_adapters_admin_context) || true
  [[ -n $org && -n $admin ]] || {
    warn 'Integration read-back needs a single organization with an active administrator.'; return 3;
  }
  state=$(company_adapters_current_state "$org" "$provider") || state=''
  [[ -n $state ]] || state=skip
  mapping=$(company_adapters_map_files "$provider")
  for entry in $mapping; do
    name=${entry%%:*}
    if [[ $state == selected ]] && ! company_adapters_env_has_key "$HOME/.config/oaos-company/.env" "$name"; then
      printf '%-28s FAIL\n' 'Organization app secret'
      printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
      return 1
    fi
  done
  [[ $(stat -c %a "$HOME/.config/oaos-company/.env") == 600 ]] || {
    printf '%-28s FAIL\n' 'EnvironmentFile permissions'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  }
  if ! psql -X -q -v ON_ERROR_STOP=1 -v provider="$provider" \
      -f "$repo_root/editions/company/adapters-readback.sql" >/dev/null 2>&1; then
    printf '%-28s FAIL\n' 'Integration response queries'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  printf '%-28s PASS\n' 'Migration SHA-256/version 6'
  printf '%-28s PASS\n' 'Selection gate and admin control'
  printf '%-28s PASS\n' 'Organization app and read-only scope'
  printf '%-28s PASS\n' 'Provider adapter responses'
  printf '%-28s PASS\n' 'Owner delegation isolation'
  printf '%-28s PASS\n' 'Revocation and SKIP cleanup'
  printf '%-28s PASS\n' 'Tenant isolation and audit trail'
  printf '%-28s PASS\n' 'Fixture rollback'
  printf 'Summary: PASS=8 FAIL=0 MANUAL=0 SKIP=0\n'
  stage_mark "$stage" verified "Integration $provider E read-back passed (state ${state}); fixture rolled back"
}

company_adapters_verify_c15() {
  local role_safe result=0 org='' admin='' provider state suite='' check
  [[ $(stage_status c01) == verified && $(stage_status c02) == verified && \
     $(stage_status c03) == verified && $(stage_status c04) == verified && \
     $(stage_status c05) == verified ]] || {
    warn 'C15 read-back needs C01, C02, C03, C04 and C05 verified on E.'; return 3;
  }
  [[ $(stage_status c15) == applied || $(stage_status c15) == verified ]] || {
    warn 'C15 has not been applied.'; return 3;
  }
  company_c02_load_settings || { warn 'C02 E deployment settings are invalid.'; return 3; }
  [[ $PGHOST == 127.0.0.1 || $PGHOST == localhost ]] || {
    warn 'C15 read-back requires loopback PostgreSQL.'; return 3;
  }
  export PGHOSTADDR=127.0.0.1 PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  have_cmd psql || { warn 'psql is required for the C15 read-back.'; return 3; }
  psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1 || {
    warn 'E database connection failed.'; return 3;
  }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'C15 database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'C15 read-back needs a non-BYPASSRLS role.'; return 3; }
  bash "$repo_root/editions/company/migrate.sh" --check --through 6 >/dev/null 2>&1 || result=$?
  if ((result)); then
    if ((result == 3)); then warn 'Integration migration is pending.'; return 3; fi
    warn 'Integration migration checksum or version read-back failed.'; return 1
  fi
  read -r org admin < <(company_adapters_admin_context) || true
  [[ -n $org && -n $admin ]] || {
    warn 'C15 read-back needs a single organization with an active administrator.'; return 3;
  }
  for provider in slack notion google_workspace microsoft_365; do
    state=$(company_adapters_sql "$org" "$admin" \
      "SELECT company.integration_selection('$provider')") || return 1
    if [[ $state == selected ]]; then
      case $provider in
        google_workspace|microsoft_365)
          if [[ -n $suite && $suite != "$provider" ]]; then
            printf '%-28s FAIL\n' 'Single productivity suite'
            printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
            return 1
          fi
          suite=$provider ;;
      esac
      check=$(company_adapters_sql "$org" "$admin" \
        "SELECT count(*) FROM company.integration_apps WHERE provider = '$provider' AND state = 'active'") || return 1
      if [[ ! $check =~ ^[1-9][0-9]*$ ]]; then
        printf '%-28s FAIL\n' 'Selected app present'
        printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
        return 1
      fi
    else
      check=$(company_adapters_sql "$org" "$admin" \
        "SELECT count(*) FROM company.integration_apps WHERE provider = '$provider' AND state = 'active'
           UNION ALL SELECT count(*) FROM company.integration_delegations WHERE provider = '$provider' AND state = 'active'") || return 1
      if [[ ! $check =~ ^0?$ ]]; then
        printf '%-28s FAIL\n' 'SKIP credential absence'
        printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
        return 1
      fi
    fi
  done
  printf '%-28s PASS\n' 'Migration SHA-256/version 6'
  printf '%-28s PASS\n' 'G11/G12 selection state'
  printf '%-28s PASS\n' 'Single productivity suite'
  printf '%-28s PASS\n' 'Selected provider app'
  printf '%-28s PASS\n' 'SKIP provider has no credential'
  printf 'Summary: PASS=5 FAIL=0 MANUAL=0 SKIP=0\n'
  stage_mark c15 verified 'C15 G11/G12 gate read-back passed; existing services only'
}
