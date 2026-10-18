#!/usr/bin/env bash
# Sourced by company-verify.sh. Uses only E loopback PostgreSQL.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/c02-verify.sh
. "$repo_root/editions/company/c02-verify.sh"

company_c06_verify() {
  local role_safe result=0
  [[ $(stage_status c01) == verified && $(stage_status c02) == verified && \
     $(stage_status c03) == verified && $(stage_status c04) == verified && \
     $(stage_status c05) == verified ]] || {
    warn 'C06 read-back needs C01, C02, C03, C04 and C05 verified on E.'; return 3;
  }
  [[ $(stage_status c06) == applied || $(stage_status c06) == verified ]] || {
    warn 'C06 has not been applied.'; return 3;
  }
  company_c02_load_settings || { warn 'C02 E deployment settings are invalid.'; return 3; }
  [[ $PGHOST == 127.0.0.1 || $PGHOST == localhost ]] || {
    warn 'C06 read-back requires loopback PostgreSQL.'; return 3;
  }
  export PGHOSTADDR=127.0.0.1 PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  have_cmd psql || { warn 'psql is required for C06 read-back.'; return 3; }
  psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1 || {
    warn 'E database connection failed.'; return 3;
  }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'C06 database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'C06 read-back needs a non-BYPASSRLS role.'; return 3; }
  bash "$repo_root/editions/company/migrate.sh" --check --through 6 >/dev/null 2>&1 || result=$?
  if ((result)); then
    if ((result == 3)); then warn 'C06 migration is pending.'; return 3; fi
    warn 'C06 migration checksum or version read-back failed.'; return 1
  fi
  if ! psql -X -q -v ON_ERROR_STOP=1 -f "$repo_root/editions/company/c06-readback.sql" >/dev/null 2>&1; then
    printf '%-28s FAIL\n' 'Identity binding queries'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  printf '%-28s PASS\n' 'Migration SHA-256/version 6'
  printf '%-28s PASS\n' 'Owner identity binding'
  printf '%-28s PASS\n' 'Provider mismatch denied'
  printf '%-28s PASS\n' 'Cross-owner binding denied'
  printf '%-28s PASS\n' 'Credential revocation blocks'
  printf '%-28s PASS\n' 'Assistant revocation and audit'
  printf '%-28s PASS\n' 'Tenant isolation and rollback'
  printf 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=0\n'
  stage_mark c06 verified 'C06 E identity isolation SQL responses passed; fixture rolled back'
}
