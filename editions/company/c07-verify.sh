#!/usr/bin/env bash
# Sourced by company-verify.sh. Uses only E loopback PostgreSQL.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/c02-verify.sh
. "$repo_root/editions/company/c02-verify.sh"

company_c07_verify() {
  local role_safe result=0
  [[ $(stage_status c01) == verified && $(stage_status c02) == verified && \
     $(stage_status c03) == verified && $(stage_status c04) == verified && \
     $(stage_status c05) == verified && $(stage_status c06) == verified ]] || {
    warn 'C07 read-back needs C01, C02, C03, C04, C05 and C06 verified on E.'; return 3;
  }
  [[ $(stage_status c07) == applied || $(stage_status c07) == verified ]] || {
    warn 'C07 has not been applied.'; return 3;
  }
  company_c02_load_settings || { warn 'C02 E deployment settings are invalid.'; return 3; }
  [[ $PGHOST == 127.0.0.1 || $PGHOST == localhost ]] || {
    warn 'C07 read-back requires loopback PostgreSQL.'; return 3;
  }
  export PGHOSTADDR=127.0.0.1 PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  have_cmd psql || { warn 'psql is required for C07 read-back.'; return 3; }
  psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1 || {
    warn 'E database connection failed.'; return 3;
  }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'C07 database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'C07 read-back needs a non-BYPASSRLS role.'; return 3; }
  bash "$repo_root/editions/company/migrate.sh" --check --through 8 >/dev/null 2>&1 || result=$?
  if ((result)); then
    if ((result == 3)); then warn 'C07 migration is pending.'; return 3; fi
    warn 'C07 migration checksum or version read-back failed.'; return 1
  fi
  if ! psql -X -q -v ON_ERROR_STOP=1 -f "$repo_root/editions/company/c07-readback.sql" >/dev/null 2>&1; then
    printf '%-28s FAIL\n' 'Policy and approval queries'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  printf '%-28s PASS\n' 'Migration SHA-256/version 8'
  printf '%-28s PASS\n' 'Default deny and explicit allow'
  printf '%-28s PASS\n' 'Non-admin and self-approval denied'
  printf '%-28s PASS\n' 'Binding, expiry and single-use reuse'
  printf '%-28s PASS\n' 'Policy version invalidation'
  printf '%-28s PASS\n' 'Admin/member audit boundary'
  printf '%-28s PASS\n' 'Sensitive metadata and tenant isolation'
  printf 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=0\n'
  stage_mark c07 verified 'C07 E policy, JIT approval and audit SQL responses passed; fixture rolled back'
}
