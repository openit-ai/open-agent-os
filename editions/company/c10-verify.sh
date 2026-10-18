#!/usr/bin/env bash
# Sourced by company-verify.sh. Uses the C02 E host and loopback PostgreSQL.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/c02-verify.sh
. "$repo_root/editions/company/c02-verify.sh"

company_c10_verify() {
  local role_safe result=0 env_file setup_file unit_file setup_url health
  [[ $(stage_status c02) == verified && $(stage_status c03) == verified ]] || {
    warn 'C10 read-back needs C02 and C03 verified on E.'; return 3;
  }
  [[ $(stage_status c10) == applied || $(stage_status c10) == verified ]] || {
    warn 'C10 has not been applied.'; return 3;
  }
  company_c02_load_settings || { warn 'C02 E deployment settings are invalid.'; return 3; }
  [[ $PGHOST == 127.0.0.1 || $PGHOST == localhost ]] || {
    warn 'C10 read-back requires loopback PostgreSQL.'; return 3;
  }
  [[ ${OAOS_COMPANY_HTTPS_ORIGIN:-} =~ ^https://[A-Za-z0-9.-]+$ ]] || {
    warn 'C10 read-back requires the real HTTPS origin.'; return 3;
  }
  export PGHOSTADDR=127.0.0.1 PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  have_cmd psql || { warn 'psql is required for C10 read-back.'; return 3; }
  psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1 || {
    warn 'E database connection failed.'; return 3;
  }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'C10 database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'C10 read-back needs a non-BYPASSRLS role.'; return 3; }
  bash "$repo_root/editions/company/migrate.sh" --check --through 11 >/dev/null 2>&1 || result=$?
  if ((result)); then
    if ((result == 3)); then warn 'C10 migration is pending.'; return 3; fi
    warn 'C10 migration checksum or version read-back failed.'; return 1
  fi

  env_file="$HOME/.config/oaos-company/.env"
  setup_file="$HOME/.config/oaos-company/console.conf"
  unit_file="$HOME/.config/systemd/user/oaos-company.service"
  [[ -f $env_file && $(stat -c %a "$env_file") == 600 ]] || {
    printf '%-30s FAIL\n' 'Console EnvironmentFile'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  }
  grep -Eq '^OAOS_COMPANY_CONSOLE_SECRET=[A-Za-z0-9+/=]{40,}$' "$env_file" &&
    grep -Fxq "OAOS_COMPANY_HTTPS_ORIGIN=${OAOS_COMPANY_HTTPS_ORIGIN}" "$env_file" || {
    printf '%-30s FAIL\n' 'Console secret and HTTPS origin'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  }
  [[ -f $setup_file && $(stat -c %a "$setup_file") == 600 ]] || {
    printf '%-30s FAIL\n' 'G10 setup link file'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  }
  setup_url=$(sed -n 's/^OAOS_COMPANY_SETUP_URL=//p' "$setup_file")
  [[ $setup_url == "${OAOS_COMPANY_HTTPS_ORIGIN}/company/setup" &&
     $setup_url != *'?'* && $setup_url != *'token'* && $setup_url != *'code='* ]] || {
    printf '%-30s FAIL\n' 'HTTPS G10 setup link'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  }
  [[ -f $HOME/.local/lib/oaos-company/console_app.py &&
     -f $HOME/.local/lib/oaos-company/project_api.py ]] || {
    printf '%-30s FAIL\n' 'Console application files'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  }
  grep -Fq 'ExecStart=/usr/bin/python3 ' "$unit_file" 2>/dev/null &&
    grep -Fq 'console_app.py' "$unit_file" 2>/dev/null || {
    printf '%-30s FAIL\n' 'Console unit ExecStart'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  }
  [[ $(systemctl --user is-enabled oaos-company.service 2>/dev/null || true) == enabled &&
     $(systemctl --user is-active oaos-company.service 2>/dev/null || true) == active ]] || {
    printf '%-30s FAIL\n' 'Console service active'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  }
  health=$(curl --silent --show-error --noproxy '*' --max-time 8 \
    http://127.0.0.1:8765/company/health 2>/dev/null || true)
  [[ ${health//[$'\r\n']/} == ok ]] || {
    printf '%-30s FAIL\n' 'Console loopback health'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  }
  if ! psql -X -q -v ON_ERROR_STOP=1 \
      -f "$repo_root/editions/company/c10-readback.sql" >/dev/null 2>&1; then
    printf '%-30s FAIL\n' 'G10 setup and login queries'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  printf '%-30s PASS\n' 'Migration SHA-256/version 11'
  printf '%-30s PASS\n' 'HTTPS G10 setup link'
  printf '%-30s PASS\n' 'Console service and loopback health'
  printf '%-30s PASS\n' 'One-time administrator code'
  printf '%-30s PASS\n' 'Two member mappings and resume'
  printf '%-30s PASS\n' 'One-time login and 8-hour session'
  printf '%-30s PASS\n' 'CSRF, expiry, lockout and revocation'
  printf '%-30s PASS\n' 'Audit trail without credential material'
  printf 'Summary: PASS=8 FAIL=0 MANUAL=0 SKIP=0\n'
  stage_mark c10 verified 'C10 E G10 setup/login SQL responses passed; fixture rolled back'
}
