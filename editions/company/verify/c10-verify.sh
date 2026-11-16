#!/usr/bin/env bash
# Sourced by company-verify.sh. Uses the C02 E host and loopback PostgreSQL.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/verify/c02-verify.sh
. "$repo_root/editions/company/verify/c02-verify.sh"

company_c10_verify() {
  local role_safe result=0 env_file setup_file unit_file setup_url health
  [[ $(stage_status c02) == verified && $(stage_status c03) == verified &&
     $(stage_status c09) == verified ]] || {
    warn 'C10 read-back needs C02, C03 and C09 verified on E.'; return 3;
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
  bash "$repo_root/editions/company/lib/migrate.sh" --check --through 11 >/dev/null 2>&1 || result=$?
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
  # Test-only console switches must never reach the production EnvironmentFile.
  if grep -Eq '^(OAOS_COMPANY_COOKIE_SECURE|OAOS_COMPANY_CONSOLE_TEST)=' "$env_file"; then
    printf '%-30s FAIL\n' 'No test-only console switches'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
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
  # The G10 link must answer through this instance's nginx/TLS path, not only on
  # loopback HTTP: anchor the origin host to 127.0.0.1 as C02 does.
  local https_host https_code https_body
  https_host=${OAOS_COMPANY_HTTPS_ORIGIN#https://}
  https_body=$(mktemp)
  https_code=$(curl --silent --show-error --noproxy '*' --max-time 8 \
    --resolve "$https_host:443:127.0.0.1" --output "$https_body" --write-out '%{http_code}' \
    "$setup_url" 2>/dev/null || true)
  if [[ $https_code != 200 ]] || ! grep -Eq 'G10|Company' "$https_body"; then
    rm -f -- "$https_body"
    printf '%-30s FAIL\n' 'HTTPS G10 setup link'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  rm -f -- "$https_body"
  # Real G10 state of this single-organization E (maintenance path, read-only).
  local evidence complete=f login=f mapped=0
  evidence=$(company_maint psql --host="$(company_maint_socket)" \
    --port="${PGPORT:-5432}" --dbname="$PGDATABASE" -X -q -A -t -F '|' -c "
      SELECT EXISTS (SELECT 1 FROM company.console_setup WHERE state = 'complete'),
             EXISTS (SELECT 1 FROM company.audit_events e
                       JOIN company.members m ON m.org_id = e.org_id AND m.id = e.actor_id
                      WHERE m.role = 'admin' AND m.state = 'active'
                        AND ((e.event_type = 'console_login' AND e.action = 'authenticated')
                          OR (e.event_type = 'console_setup' AND e.action = 'admin_created'))),
             (SELECT count(*) FROM company.members WHERE role = 'member' AND state = 'active')" \
    2>/dev/null) || evidence=''
  IFS='|' read -r complete login mapped <<< "${evidence%%$'\n'*}"
  printf '%-30s PASS\n' 'Migration SHA-256/version 11'
  printf '%-30s PASS\n' 'HTTPS G10 setup link'
  printf '%-30s PASS\n' 'Console service and loopback health'
  if [[ $complete != t ]]; then
    # The SQL contract creates a fixture organization, which the single-org
    # G10 flow only accepts before the real setup completes.
    if ! psql -X -q -v ON_ERROR_STOP=1 \
        -f "$repo_root/editions/company/readback/c10-readback.sql" >/dev/null 2>&1; then
      printf '%-30s FAIL\n' 'G10 setup/login SQL contract'
      printf 'Summary: PASS=3 FAIL=1 MANUAL=0 SKIP=0\n'
      return 1
    fi
    printf '%-30s PASS\n' 'G10 setup/login SQL contract'
    (umask 077; date -u +%Y-%m-%dT%H:%M:%SZ > "$HOME/.config/oaos-company/g10-contract-passed")
    printf '%-30s MANUAL\n' 'Real admin login and G10 done'
    printf 'Summary: PASS=4 FAIL=0 MANUAL=1 SKIP=0\n'
    stage_mark c10 applied 'C10 G10 SQL contract passed; real administrator login over HTTPS pending'
    info 'Open the HTTPS G10 link, confirm the administrator and two mappings, then rerun the read-back.'
    return 3
  fi
  # The contract must have passed on this E before G10 completed (MANUAL run).
  if [[ ! -s $HOME/.config/oaos-company/g10-contract-passed ]]; then
    printf '%-30s FAIL\n' 'G10 SQL contract before setup'
    printf 'Summary: PASS=3 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  printf '%-30s PASS\n' 'G10 SQL contract before setup'
  if [[ $login != t || ! $mapped =~ ^[0-9]+$ ]] || ((mapped < 2)); then
    printf '%-30s FAIL\n' 'Real admin login and mappings'
    printf 'Summary: PASS=4 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  printf '%-30s PASS\n' 'Real admin login and G10 done'
  printf '%-30s PASS\n' 'Two member mappings'
  printf 'Summary: PASS=6 FAIL=0 MANUAL=0 SKIP=0\n'
  stage_mark c10 verified 'C10 E G10 completed with a real administrator login over the HTTPS link; SQL contract passed'
}
