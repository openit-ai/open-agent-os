#!/usr/bin/env bash
# Sourced by company-verify.sh. Uses only E loopback PostgreSQL.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/c02-verify.sh
. "$repo_root/editions/company/c02-verify.sh"

company_c03_verify() {
  local role_safe result=0
  [[ $(stage_status c01) == verified && $(stage_status c02) == verified ]] || {
    warn 'C03 read-back needs C01 and C02 verified on E.'; return 3;
  }
  [[ $(stage_status c03) == applied || $(stage_status c03) == verified ]] || {
    warn 'C03 has not been applied.'; return 3;
  }
  company_c02_load_settings || { warn 'C02 E deployment settings are invalid.'; return 3; }
  [[ $PGHOST == 127.0.0.1 || $PGHOST == localhost ]] || {
    warn 'C03 read-back requires loopback PostgreSQL.'; return 3;
  }
  export PGHOSTADDR=127.0.0.1 PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  have_cmd psql || { warn 'psql is required for C03 read-back.'; return 3; }
  psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1 || {
    warn 'E database connection failed.'; return 3;
  }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'C03 database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'C03 read-back needs a non-BYPASSRLS role.'; return 3; }
  bash "$repo_root/editions/company/migrate.sh" --check --through 2 >/dev/null 2>&1 || result=$?
  if ((result)); then
    if ((result == 3)); then warn 'C03 migration is pending.'; return 3; fi
    warn 'C03 migration checksum or version read-back failed.'; return 1
  fi
  if ! psql -X -q -v ON_ERROR_STOP=1 -f "$repo_root/editions/company/c03-readback.sql" >/dev/null 2>&1; then
    printf '%-28s FAIL\n' 'Mapping response queries'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  printf '%-28s PASS\n' 'Migration SHA-256/version 2'
  printf '%-28s PASS\n' 'Admin and member lookup'
  printf '%-28s PASS\n' 'External lookup and duplicate'
  printf '%-28s PASS\n' 'Unknown, suspended, departed'
  printf '%-28s PASS\n' 'Tenant isolation and rollback'
  printf 'Summary: PASS=5 FAIL=0 MANUAL=0 SKIP=0\n'
  stage_mark c03 verified 'C03 E SQL mapping responses passed; fixture rolled back'
}
