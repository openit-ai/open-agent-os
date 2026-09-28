#!/usr/bin/env bash
# Sourced by company-verify.sh. Uses only E loopback PostgreSQL.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/c02-verify.sh
. "$repo_root/editions/company/c02-verify.sh"

company_c04_verify() {
  local role_safe result=0
  [[ $(stage_status c01) == verified && $(stage_status c02) == verified && $(stage_status c03) == verified ]] || {
    warn 'C04 read-back needs C01, C02 and C03 verified on E.'; return 3;
  }
  [[ $(stage_status c04) == applied || $(stage_status c04) == verified ]] || {
    warn 'C04 has not been applied.'; return 3;
  }
  company_c02_load_settings || { warn 'C02 E deployment settings are invalid.'; return 3; }
  [[ $PGHOST == 127.0.0.1 || $PGHOST == localhost ]] || {
    warn 'C04 read-back requires loopback PostgreSQL.'; return 3;
  }
  export PGHOSTADDR=127.0.0.1 PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  have_cmd psql || { warn 'psql is required for C04 read-back.'; return 3; }
  psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1 || {
    warn 'E database connection failed.'; return 3;
  }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'C04 database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'C04 read-back needs a non-BYPASSRLS role.'; return 3; }
  bash "$repo_root/editions/company/migrate.sh" --check --through 3 >/dev/null 2>&1 || result=$?
  if ((result)); then
    if ((result == 3)); then warn 'C04 migration is pending.'; return 3; fi
    warn 'C04 migration checksum or version read-back failed.'; return 1
  fi
  if ! psql -X -q -v ON_ERROR_STOP=1 -f "$repo_root/editions/company/c04-readback.sql" >/dev/null 2>&1; then
    printf '%-28s FAIL\n' 'Assistant response queries'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  printf '%-28s PASS\n' 'Migration SHA-256/version 3'
  printf '%-28s PASS\n' 'Two assistant namespaces'
  printf '%-28s PASS\n' 'Knowledge/output isolation'
  printf '%-28s PASS\n' 'Revocation and inactive owner'
  printf '%-28s PASS\n' 'Tenant isolation and rollback'
  printf 'Summary: PASS=5 FAIL=0 MANUAL=0 SKIP=0\n'
  stage_mark c04 verified 'C04 E assistant SQL responses passed; fixture rolled back'
}
