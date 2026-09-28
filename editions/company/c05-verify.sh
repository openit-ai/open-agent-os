#!/usr/bin/env bash
# Sourced by company-verify.sh. Uses only E loopback PostgreSQL.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/c02-verify.sh
. "$repo_root/editions/company/c02-verify.sh"

company_c05_verify() {
  local role_safe result=0
  [[ $(stage_status c01) == verified && $(stage_status c02) == verified && $(stage_status c03) == verified && $(stage_status c04) == verified ]] || {
    warn 'C05 read-back needs C01, C02, C03 and C04 verified on E.'; return 3;
  }
  [[ $(stage_status c05) == applied || $(stage_status c05) == verified ]] || {
    warn 'C05 has not been applied.'; return 3;
  }
  company_c02_load_settings || { warn 'C02 E deployment settings are invalid.'; return 3; }
  python3 "$repo_root/editions/company/vault.py" check-key \
    "$HOME/.config/oaos-company/.env" || return 3
  [[ $PGHOST == 127.0.0.1 || $PGHOST == localhost ]] || {
    warn 'C05 read-back requires loopback PostgreSQL.'; return 3;
  }
  export PGHOSTADDR=127.0.0.1 PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  have_cmd psql || { warn 'psql is required for C05 read-back.'; return 3; }
  psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1 || {
    warn 'E database connection failed.'; return 3;
  }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'C05 database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'C05 read-back needs a non-BYPASSRLS role.'; return 3; }
  bash "$repo_root/editions/company/migrate.sh" --check --through 4 >/dev/null 2>&1 || result=$?
  if ((result)); then
    if ((result == 3)); then warn 'C05 migration is pending.'; return 3; fi
    warn 'C05 migration checksum or version read-back failed.'; return 1
  fi
  if ! psql -X -q -v ON_ERROR_STOP=1 -f "$repo_root/editions/company/c05-readback.sql" >/dev/null 2>&1; then
    printf '%-28s FAIL\n' 'Credential response queries'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  printf '%-28s PASS\n' 'Migration SHA-256/version 4'
  printf '%-28s PASS\n' 'Restricted AES-GCM key'
  printf '%-28s PASS\n' 'Owner envelope and use'
  printf '%-28s PASS\n' 'Other owner denied'
  printf '%-28s PASS\n' 'Rotation and old generation'
  printf '%-28s PASS\n' 'Revocation and audit history'
  printf '%-28s PASS\n' 'Tenant isolation and rollback'
  printf 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=0\n'
  stage_mark c05 verified 'C05 E personal credential SQL responses passed; fixture rolled back'
}
