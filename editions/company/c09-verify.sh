#!/usr/bin/env bash
# Sourced by company-verify.sh. Uses only E loopback PostgreSQL and the local
# console port. Never prints session tokens or EnvironmentFile values.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/c02-verify.sh
. "$repo_root/editions/company/c02-verify.sh"

company_c09_verify() {
  local role_safe result=0 console_port host body
  [[ $(stage_status c01) == verified && $(stage_status c02) == verified && \
     $(stage_status c03) == verified && $(stage_status c07) == verified ]] || {
    warn 'C09 read-back needs C01, C02, C03 and C07 verified on E.'; return 3;
  }
  [[ $(stage_status c09) == applied || $(stage_status c09) == verified ]] || {
    warn 'C09 has not been applied.'; return 3;
  }
  company_c02_load_settings || { warn 'C02 E deployment settings are invalid.'; return 3; }
  [[ $PGHOST == 127.0.0.1 || $PGHOST == localhost ]] || {
    warn 'C09 read-back requires loopback PostgreSQL.'; return 3;
  }
  export PGHOSTADDR=127.0.0.1 PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  have_cmd psql || { warn 'psql is required for C09 read-back.'; return 3; }
  psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1 || {
    warn 'E database connection failed.'; return 3;
  }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'C09 database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'C09 read-back needs a non-BYPASSRLS role.'; return 3; }
  bash "$repo_root/editions/company/migrate.sh" --check --through 10 >/dev/null 2>&1 || result=$?
  if ((result)); then
    if ((result == 3)); then warn 'C09 migration is pending.'; return 3; fi
    warn 'C09 migration checksum or version read-back failed.'; return 1
  fi
  if [[ $(systemctl --user is-active oaos-company.service 2>/dev/null || true) != active ]]; then
    printf '%-32s FAIL\n' 'Console service active'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  have_cmd curl || { warn 'curl is required for the C09 health read-back.'; return 3; }
  console_port=${OAOS_COMPANY_CONSOLE_PORT:-8765}
  [[ $console_port =~ ^[0-9]{4,5}$ ]] || { warn 'C09 console port is invalid.'; return 3; }
  host=127.0.0.1
  body=$(curl --silent --show-error --noproxy '*' --max-time 8 \
    "http://$host:$console_port/company/health" 2>/dev/null || true)
  if [[ ${body//[$'\r\n']/} != ok ]]; then
    printf '%-32s FAIL\n' 'Console health endpoint'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  if ! psql -X -q -v ON_ERROR_STOP=1 -f "$repo_root/editions/company/c09-readback.sql" >/dev/null 2>&1; then
    printf '%-32s FAIL\n' 'Console tab and permission queries'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  printf '%-32s PASS\n' 'Migration SHA-256/version 10'
  printf '%-32s PASS\n' 'Console service active'
  printf '%-32s PASS\n' 'Console health endpoint'
  printf '%-32s PASS\n' 'Admin four-tab snapshots'
  printf '%-32s PASS\n' 'Member scope limited'
  printf '%-32s PASS\n' 'Secret metadata masking'
  printf '%-32s PASS\n' 'Session token expiry and revocation'
  printf '%-32s PASS\n' 'Tenant isolation and fixture rollback'
  printf 'Summary: PASS=8 FAIL=0 MANUAL=0 SKIP=0\n'
  stage_mark c09 verified 'C09 E console tab, role, masking and service responses passed; fixture rolled back'
}
