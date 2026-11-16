#!/usr/bin/env bash
# Sourced by company-verify.sh. Uses only E loopback PostgreSQL.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/stages/adapters.sh
. "$repo_root/editions/company/stages/adapters.sh"

company_adapters_verify() {
  local provider=$1 stage=$2 role_safe result=0 org='' admin='' state='' mapping entry name
  local missing roundtrip env_file="$HOME/.config/oaos-company/.env"
  missing=$(company_adapters_dependencies_met "$provider") || {
    warn "Integration read-back needs $missing verified on E."; return 3;
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
  bash "$repo_root/editions/company/lib/migrate.sh" --check --through 7 >/dev/null 2>&1 || result=$?
  if ((result)); then
    if ((result == 3)); then warn 'Integration migration is pending.'; return 3; fi
    warn 'Integration migration checksum or version read-back failed.'; return 1
  fi
  read -r org admin < <(company_admin_context) || true
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
  company_env_file_ok || {
    printf '%-28s FAIL\n' 'EnvironmentFile permissions'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  }
  if ! psql -X -q -v ON_ERROR_STOP=1 -v provider="$provider" \
      -f "$repo_root/editions/company/readback/adapters-readback.sql" >/dev/null 2>&1; then
    printf '%-28s FAIL\n' 'Integration response queries'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  # The SQL above proves the DB contract with fixtures; it is not evidence that
  # the organization's real service answered.
  printf '%-28s PASS\n' 'Migration SHA-256/version 7'
  printf '%-28s PASS\n' 'DB contract: selection/admin'
  printf '%-28s PASS\n' 'DB contract: app and scopes'
  printf '%-28s PASS\n' 'DB contract: owner isolation'
  printf '%-28s PASS\n' 'DB contract: revocation/audit'
  if [[ $state != selected ]]; then
    # Not selected: SKIP(reason=not_selected) with no credential anywhere.
    if ! company_adapters_keys_absent "$provider" "$env_file"; then
      printf '%-28s FAIL\n' 'SKIP has no app credential'
      printf 'Summary: PASS=5 FAIL=1 MANUAL=0 SKIP=0\n'
      return 1
    fi
    printf '%-28s PASS\n' 'SKIP has no app credential'
    printf '%-28s SKIP\n' 'External round-trip'
    printf 'Summary: PASS=6 FAIL=0 MANUAL=0 SKIP=1\n'
    stage_mark "$stage" SKIP "not_selected: $provider has no credential, webhook or service call"
    return 0
  fi
  printf '%-28s PASS\n' 'Organization app secret'
  roundtrip=$(company_adapters_roundtrip_count "$org" "$admin" "$provider") || roundtrip=0
  if [[ ! $roundtrip =~ ^[1-9][0-9]*$ ]]; then
    printf '%-28s MANUAL\n' 'External round-trip'
    printf 'Summary: PASS=6 FAIL=0 MANUAL=1 SKIP=0\n'
    info "Integration $provider stays applied until a real round-trip with the organization's service is recorded."
    return 3
  fi
  printf '%-28s PASS\n' 'External round-trip'
  printf 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=0\n'
  stage_mark "$stage" verified "Integration $provider E read-back passed with a recorded external round-trip"
}

company_adapters_verify_c15() {
  local role_safe result=0 org='' admin='' provider state suite='' check
  local stage env_file="$HOME/.config/oaos-company/.env"
  [[ $(stage_status c10) == verified ]] || { warn 'C15 read-back needs C10 verified on E.'; return 3; }
  for stage in c11 c12 c13 c14; do
    [[ $(stage_status "$stage") == verified || $(stage_status "$stage") == SKIP ]] || {
      warn "C15 read-back needs $stage verified or SKIP."; return 3;
    }
  done
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
  bash "$repo_root/editions/company/lib/migrate.sh" --check --through 7 >/dev/null 2>&1 || result=$?
  if ((result)); then
    if ((result == 3)); then warn 'Integration migration is pending.'; return 3; fi
    warn 'Integration migration checksum or version read-back failed.'; return 1
  fi
  read -r org admin < <(company_admin_context) || true
  [[ -n $org && -n $admin ]] || {
    warn 'C15 read-back needs a single organization with an active administrator.'; return 3;
  }
  for provider in slack notion google_workspace microsoft_365; do
    state=$(company_sql "$org" "$admin" \
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
      check=$(company_sql "$org" "$admin" \
        "SELECT count(*) FROM company.integration_apps WHERE provider = '$provider' AND state = 'active'") || return 1
      if [[ ! $check =~ ^[1-9][0-9]*$ ]]; then
        printf '%-28s FAIL\n' 'Selected app present'
        printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
        return 1
      fi
    else
      check=$(company_sql "$org" "$admin" \
        "SELECT count(*) FROM company.integration_apps WHERE provider = '$provider' AND state = 'active'
           UNION ALL SELECT count(*) FROM company.integration_delegations WHERE provider = '$provider' AND state = 'active'") || return 1
      if [[ ! $check =~ ^0?$ ]] || ! company_adapters_keys_absent "$provider" "$env_file"; then
        printf '%-28s FAIL\n' 'SKIP credential absence'
        printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
        return 1
      fi
    fi
  done
  printf '%-28s PASS\n' 'Migration SHA-256/version 7'
  printf '%-28s PASS\n' 'G11/G12 selection state'
  printf '%-28s PASS\n' 'Single productivity suite'
  printf '%-28s PASS\n' 'Selected provider app'
  printf '%-28s PASS\n' 'SKIP: no DB or .env credential'
  printf 'Summary: PASS=5 FAIL=0 MANUAL=0 SKIP=0\n'
  stage_mark c15 verified 'C15 G11/G12 gate read-back passed; existing services only'
}
