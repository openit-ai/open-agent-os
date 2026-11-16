#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
install="$repo_root/editions/company/install.sh"
verify="$repo_root/bootstrap/verify/company-verify.sh"
mkdir -p "$temp_root/home/.oaos-install" "$temp_root/bin" "$temp_root/backup" "$temp_root/key-backup" "$temp_root/rollback"
chmod 700 "$temp_root/backup" "$temp_root/key-backup" "$temp_root/rollback"
cat > "$temp_root/vhost.conf" <<'EOF'
server {
  listen 443 ssl;
  server_name e.example.test;
  ssl_certificate /etc/letsencrypt/live/e.example.test/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/e.example.test/privkey.pem;
  location / { return 200; }
}
EOF
python3 - "$temp_root/home/.oaos-install/company-state.json" <<'PY'
import json, sys
with open(sys.argv[1], 'w', encoding='utf-8') as out:
    json.dump({'edition':'company','stages':{'c01':{'status':'verified'}}}, out)
PY
cat > "$temp_root/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ ${1:-} == --user ]]; then
  shift
  case $1 in
    show-environment) [[ ${OAOS_C02_NO_USER_MANAGER:-0} != 1 ]] ;;
    daemon-reload|enable) exit 0 ;;
    list-unit-files) exit 0 ;;
    is-enabled) printf 'enabled\n' ;;
    is-active) printf 'active\n' ;;
    start)
      # Emulate systemd: only the unit's EnvironmentFile reaches ExecStart.
      unit="$HOME/.config/systemd/user/$2"
      env_file=$(sed -n 's/^EnvironmentFile=//p' "$unit")
      exec_start=$(sed -n 's/^ExecStart=//p' "$unit")
      ! grep -Fxq 'NoNewPrivileges=yes' "$unit" || { printf 'sudo blocked by NoNewPrivileges\n' >&2; exit 1; }
      mapfile -t vars < <(grep -E '^[A-Z_]+=' "$env_file")
      env -i HOME="$HOME" PATH="$PATH" "${vars[@]}" $exec_start ;;
    show) printf 'success\n' ;;
  esac
else
  case $1 in
    is-enabled) printf 'enabled\n' ;;
    is-active) printf 'active\n' ;;
    reload) printf 'reload\n' >> "$OAOS_C02_CALLS" ;;
  esac
fi
EOF
cat > "$temp_root/bin/loginctl" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
case $1 in
  show-user) printf 'yes\n' ;;
  enable-linger) exit 0 ;;
esac
EOF
cat > "$temp_root/bin/nginx" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'test\n' >> "$OAOS_C02_CALLS"
[[ ${OAOS_C02_NGINX_FAIL:-0} != 1 ]]
EOF
cat > "$temp_root/bin/pg_dump" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
for arg in "$@"; do
  if [[ $arg == --file=* ]]; then printf 'mock custom dump\n' > "${arg#--file=}"; exit 0; fi
done
# The daily job must dump as the postgres maintenance account (FORCE RLS).
[[ ${OAOS_C02_AS_POSTGRES:-0} == 1 ]] || { printf 'query would be affected by row-level security policy\n' >&2; exit 1; }
printf 'mock custom dump\n'
EOF
cat > "$temp_root/bin/runuser" <<'EOF'
#!/usr/bin/env bash
[[ ${1:-} == -u && ${2:-} == postgres && ${3:-} == -- ]] || exit 1
shift 3
OAOS_C02_AS_POSTGRES=1 exec "$@"
EOF
cat > "$temp_root/bin/pg_restore" <<'EOF'
#!/usr/bin/env bash
[[ ${1:-} == --list && $(head -c 16 "${2:-/nonexistent}" 2>/dev/null) == 'mock custom dump' ]]
EOF
cat > "$temp_root/bin/openssl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > "$temp_root/bin/sudo" <<'EOF'
#!/usr/bin/env bash
shift
"$@"
EOF
cat > "$temp_root/bin/ss" <<'EOF'
#!/usr/bin/env bash
printf 'State Recv-Q Send-Q Local Address:Port Peer Address:Port\nLISTEN 0 5 127.0.0.1:8765 0.0.0.0:*\n'
EOF
cat > "$temp_root/bin/curl" <<'EOF'
#!/usr/bin/env bash
[[ $* == *'--resolve e.example.test:443:127.0.0.1'* && $* == *'https://e.example.test/company/health'* ]] || exit 2
if [[ ${OAOS_C02_CURL_FAIL:-0} == 1 ]]; then printf 'unavailable\n'; else printf 'ok\n'; fi
EOF
cat > "$temp_root/bin/uname" <<'EOF'
#!/usr/bin/env bash
if [[ ${OAOS_C02_NOT_LINUX:-0} == 1 ]]; then printf 'Darwin\n'; else /usr/bin/uname "$@"; fi
EOF
cat > "$temp_root/bin/getent" <<'EOF'
#!/usr/bin/env bash
if [[ $1 == passwd ]]; then printf '%s:x:1000:1000::%s:/bin/bash\n' "$2" "$HOME"; else /usr/bin/getent "$@"; fi
EOF
chmod 700 "$temp_root/bin/"*
export OAOS_C02_CALLS="$temp_root/calls"
export OAOS_COMPANY_USER
OAOS_COMPANY_USER=$(id -un)
export OAOS_COMPANY_E_ID=E-test PGDATABASE=company_e PGHOST=127.0.0.1
export OAOS_COMPANY_BACKUP_DIR="$temp_root/backup" OAOS_COMPANY_KEY_BACKUP_DIR="$temp_root/key-backup"
export OAOS_COMPANY_ROLLBACK_DIR="$temp_root/rollback" OAOS_COMPANY_NGINX_VHOST="$temp_root/vhost.conf"
export OAOS_COMPANY_HTTPS_ORIGIN=https://e.example.test
export HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" OAOS_NO_LOG_FILE=1
unset HERMES_HOME
code=0
bash "$install" --stage c02 --dry-run > "$temp_root/dry" || code=$?
[[ $code == 0 && ! -e $temp_root/calls ]] || fail 'dry-run made changes'
code=0
bash "$install" --stage c18 --dry-run >/dev/null 2>&1 || code=$?
[[ $code == 1 ]] || fail 'invalid stage accepted'
code=0
bash "$install" --stage c02 --stage c02 >/dev/null 2>&1 || code=$?
[[ $code == 1 ]] || fail 'duplicate stage accepted'
code=0
OAOS_COMPANY_USER=wrong bash "$install" --stage c02 >/dev/null 2>&1 || code=$?
[[ $code == 3 ]] || fail 'dedicated account gate'
code=0
OAOS_C02_NOT_LINUX=1 bash "$install" --stage c02 >/dev/null 2>&1 || code=$?
[[ $code == 3 ]] || fail 'unsupported OS BLOCKED'
code=0
OAOS_C02_NO_USER_MANAGER=1 bash "$install" --stage c02 >/dev/null 2>&1 || code=$?
[[ $code == 3 ]] || fail 'missing user manager BLOCKED'
code=0
bash "$install" --stage c02 > "$temp_root/first" || code=$?
[[ $code == 0 ]] || fail 'C02 install mock'
grep -Fq 'location /company/' "$temp_root/vhost.conf" || fail 'Company nginx route'
grep -Fq 'location = /company/oauth/callback' "$temp_root/vhost.conf" || fail 'OAuth callback nginx route'
[[ $(stat -c %a "$HOME/.config/oaos-company/.env") == 600 ]] || fail 'environment permissions'
[[ $(stat -c %a "$HOME/.config/oaos-company") == 700 ]] || fail 'environment directory permissions'
python3 - "$HOME/.oaos-install/company-state.json" <<'PY' || fail 'applied state'
import json, sys
assert json.load(open(sys.argv[1], encoding='utf-8'))['stages']['c02']['status'] == 'applied'
PY
first_hash=$(sha256sum "$temp_root/vhost.conf")
bash "$install" --stage c02 > /dev/null || fail 'rerun'
[[ $(sha256sum "$temp_root/vhost.conf") == "$first_hash" ]] || fail 'nginx route changed on rerun'
[[ $(grep -Fc 'location /company/' "$temp_root/vhost.conf") == 1 ]] || fail 'duplicate route'
[[ $(grep -Fc 'reload' "$OAOS_C02_CALLS") == 1 ]] || fail 'unneeded reload'
code=0
bash "$verify" --phase c02 --read-back > "$temp_root/verify-before" || code=$?
[[ $code == 3 ]] || fail 'same-boot read-back should wait'
printf 'prior-boot-id\n' > "$HOME/.config/oaos-company/initial-boot-id"
env -i PATH="$PATH" HOME="$HOME" OAOS_C02_CALLS="$OAOS_C02_CALLS" OAOS_NO_LOG_FILE=1 bash "$verify" --phase c02 --read-back > "$temp_root/verify-after" || fail 'post-reboot read-back mock with fresh environment'
python3 - "$HOME/.oaos-install/company-state.json" <<'PY' || fail 'verified state'
import json, sys
assert json.load(open(sys.argv[1], encoding='utf-8'))['stages']['c02']['status'] == 'verified'
PY
printf 'PASS: C02 gates, install, idempotence, read-back and reboot checkpoint\n'

# Daily backup job: rotation keeps 7 daily + 4 weekly dumps and their state and
# EnvironmentFile copies, removes the rest, and keeps 0700/0600 modes.
python3 - "$temp_root/backup" "$temp_root/key-backup" <<'PY'
import datetime, os, sys
backup, keys = sys.argv[1:3]
now = datetime.datetime.now(datetime.timezone.utc)
for day in range(1, 61):
    stamp = (now - datetime.timedelta(days=day)).strftime('%Y%m%dT%H%M%SZ')
    for path in (os.path.join(backup, 'company-%s.dump' % stamp),
                 os.path.join(backup, 'company-state-%s.json' % stamp),
                 os.path.join(keys, 'company-env-%s' % stamp)):
        with open(path, 'w') as out:
            out.write('mock custom dump\n')
        os.chmod(path, 0o644)
PY
chmod 755 "$temp_root/backup"
systemctl --user start oaos-company-backup.service || fail 'backup job'
[[ $(find "$temp_root/backup" -name 'company-*.dump' ! -name '*.tmp.*' | wc -l) == 11 ]] || fail 'backup rotation 7 daily + 4 weekly'
[[ $(find "$temp_root/backup" -name 'company-state-*.json' | wc -l) == 11 ]] || fail 'state copies follow dumps'
[[ $(find "$temp_root/key-backup" -name 'company-env-*' | wc -l) == 11 ]] || fail 'EnvironmentFile copies follow dumps'
[[ $(stat -c %a "$temp_root/backup") == 700 ]] || fail 'backup directory mode'
! find "$temp_root/backup" "$temp_root/key-backup" -type f ! -perm 0600 | grep . >/dev/null || fail 'backup file modes'
latest=$(< "$temp_root/backup/last-success")
[[ -s $temp_root/backup/company-$latest.dump && -s $temp_root/key-backup/company-env-$latest ]] || fail 'latest backup set'
printf 'PASS: daily backup runs as the maintenance account with 7+4 rotation and 0700/0600 modes\n'

# A failed nginx syntax check must restore the original vhost and leave C02 unverified.
sed -i '/# OAOS Company C02 begin/,/# OAOS Company C02 end/d' "$temp_root/vhost.conf"
before=$(sha256sum "$temp_root/vhost.conf")
code=0
OAOS_C02_NGINX_FAIL=1 bash "$install" --stage c02 >/dev/null 2>&1 || code=$?
[[ $code == 1 && $(sha256sum "$temp_root/vhost.conf") == "$before" ]] || fail 'nginx rollback'
printf 'PASS: nginx syntax failure restored original TLS vhost\n'
code=0
OAOS_C02_CURL_FAIL=1 bash "$install" --stage c02 >/dev/null 2>&1 || code=$?
[[ $code == 1 && $(sha256sum "$temp_root/vhost.conf") == "$before" ]] || fail 'HTTPS health rollback'
printf 'PASS: failed HTTPS health restored original TLS vhost\n'
