#!/usr/bin/env bash
# Run with: pg_virtualenv bash -e -o pipefail bootstrap/tests/company-c09-smoke.sh
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
[[ -n ${PGPORT:-} ]] || { printf 'Temporary PostgreSQL (pg_virtualenv) is required.\n' >&2; exit 3; }
temp_root=$(mktemp -d)
console_pid=''
cleanup() {
  [[ -z $console_pid ]] || kill "$console_pid" 2>/dev/null || true
  rm -rf -- "$temp_root"
}
trap cleanup EXIT
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
install="$repo_root/editions/company/install.sh"
verify="$repo_root/bootstrap/verify/company-verify.sh"
runner="$repo_root/editions/company/migrate.sh"
port=18765
mkdir -p "$temp_root/home/.oaos-install" "$temp_root/home/.config/oaos-company" \
  "$temp_root/bin" "$temp_root/backup" "$temp_root/key-backup" "$temp_root/rollback"
chmod 700 "$temp_root/home/.config/oaos-company" "$temp_root/backup" \
  "$temp_root/key-backup" "$temp_root/rollback"
printf 'server { listen 443 ssl; }\n' > "$temp_root/vhost.conf"
python3 - "$temp_root/home/.oaos-install/company-state.json" <<'PY'
import json, sys
with open(sys.argv[1], 'w', encoding='utf-8') as out:
    json.dump({'edition': 'company', 'stages': {
        'c01': {'status': 'verified'}, 'c02': {'status': 'verified'},
        'c03': {'status': 'verified'}}}, out)
PY
cat > "$temp_root/bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
case ${1:-} in
  --user) case ${2:-} in
    show-environment) [[ ${OAOS_C09_NO_USER_MANAGER:-${OAOS_C02_NO_USER_MANAGER:-0}} != 1 ]]; exit $? ;;
    list-unit-files) exit 0 ;;
    daemon-reload) exit 0 ;;
    enable|start) exit 0 ;;
    is-enabled) printf 'enabled\n'; exit 0 ;;
    is-active) printf 'active\n'; exit 0 ;;
  esac ;;
  is-enabled) printf 'enabled\n'; exit 0 ;;
  is-active) printf 'active\n'; exit 0 ;;
esac
exit 0
MOCK
cat > "$temp_root/bin/loginctl" <<'MOCK'
#!/usr/bin/env bash
printf 'yes\n'
exit 0
MOCK
cat > "$temp_root/bin/nginx" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
cat > "$temp_root/bin/openssl" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
cat > "$temp_root/bin/ss" <<'MOCK'
#!/usr/bin/env bash
printf 'State Recv-Q Send-Q Local Address:Port Peer Address:Port\nLISTEN 0 5 127.0.0.1:8765 0.0.0.0:*\n'
MOCK
cat > "$temp_root/bin/sudo" <<'MOCK'
#!/usr/bin/env bash
[[ ${1:-} == -n ]] || exit 1
shift
"$@"
MOCK
cat > "$temp_root/bin/getent" <<'MOCK'
#!/usr/bin/env bash
if [[ ${1:-} == passwd ]]; then
  printf '%s:x:1000:1000::%s:/bin/bash\n' "$2" "$HOME"
else
  /usr/bin/getent "$@"
fi
MOCK
cat > "$temp_root/bin/runuser" <<'MOCK'
#!/usr/bin/env bash
[[ ${1:-} == -u && ${2:-} == postgres && ${3:-} == -- ]] || exit 1
shift 3
"$@"
MOCK
chmod 700 "$temp_root/bin/"*

export HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" OAOS_NO_LOG_FILE=1
export OAOS_COMPANY_USER OAOS_COMPANY_E_ID=E-c09-test
OAOS_COMPANY_USER=$(id -un)
export PGHOST=localhost PGHOSTADDR=127.0.0.1
export OAOS_COMPANY_PG_SOCKET_DIR=/tmp
export OAOS_COMPANY_NGINX_VHOST="$temp_root/vhost.conf"
export OAOS_COMPANY_BACKUP_DIR="$temp_root/backup"
export OAOS_COMPANY_KEY_BACKUP_DIR="$temp_root/key-backup"
export OAOS_COMPANY_ROLLBACK_DIR="$temp_root/rollback"
export OAOS_COMPANY_HTTPS_ORIGIN=https://e.example.test
export OAOS_COMPANY_CONSOLE_PORT="$port"
printf 'OAOS_COMPANY_USER=%s\nOAOS_COMPANY_E_ID=%s\nPGDATABASE=%s\nPGHOST=%s\nPGUSER=%s\nOAOS_COMPANY_HTTPS_ORIGIN=%s\nOAOS_COMPANY_NGINX_VHOST=%s\nOAOS_COMPANY_BACKUP_DIR=%s\nOAOS_COMPANY_KEY_BACKUP_DIR=%s\nOAOS_COMPANY_ROLLBACK_DIR=%s\n' \
  "$OAOS_COMPANY_USER" "$OAOS_COMPANY_E_ID" "$PGDATABASE" "$PGHOST" "$PGUSER" \
  "$OAOS_COMPANY_HTTPS_ORIGIN" "$OAOS_COMPANY_NGINX_VHOST" "$OAOS_COMPANY_BACKUP_DIR" \
  "$OAOS_COMPANY_KEY_BACKUP_DIR" "$OAOS_COMPANY_ROLLBACK_DIR" \
  > "$HOME/.config/oaos-company/deployment.conf"
chmod 600 "$HOME/.config/oaos-company/deployment.conf"
printf 'PGDATABASE=%s\n' "$PGDATABASE" > "$HOME/.config/oaos-company/.env"
chmod 600 "$HOME/.config/oaos-company/.env"

# --- Gates -------------------------------------------------------------------
code=0
bash "$install" --stage c09 > "$temp_root/prereq" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'C07 verified gate'
grep -Fq 'C01, C02, C03 and C07 must be verified' "$temp_root/prereq" || fail 'prerequisite gate reason'
python3 - "$HOME/.oaos-install/company-state.json" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding='utf-8') as src:
    state = json.load(src)
state['stages']['c07'] = {'status': 'verified'}
with open(path, 'w', encoding='utf-8') as out:
    json.dump(state, out)
PY
code=0
OAOS_C09_NO_USER_MANAGER=1 bash "$install" --stage c09 > "$temp_root/no-manager" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'systemd user-manager gate'
grep -Fq 'systemd user manager is unavailable' "$temp_root/no-manager" || fail 'systemd gate reason'
code=0
PGPORT=1 bash "$install" --stage c09 > "$temp_root/no-db" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'database connection gate'
grep -Fq 'E database connection failed' "$temp_root/no-db" || fail 'database gate reason'

psql -X -q -v ON_ERROR_STOP=1 -c 'CREATE ROLE c09_console_test NOSUPERUSER NOBYPASSRLS' >/dev/null
psql -X -q -v ON_ERROR_STOP=1 -c "GRANT CREATE ON DATABASE \"$PGDATABASE\" TO c09_console_test" >/dev/null
code=0
env -u PGOPTIONS bash "$install" --stage c09 > "$temp_root/bypass-role" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'RLS bypass role gate'
grep -Fq 'database role bypasses RLS' "$temp_root/bypass-role" || fail 'RLS bypass gate reason'
export PGOPTIONS='-c role=c09_console_test'
bash "$runner" --through 9 >/dev/null
printf 'PASS: C09 gates and reserved migration chain\n'

# --- Install and service -----------------------------------------------------
bash "$install" --stage c09 > "$temp_root/installed" || fail 'C09 install'
[[ $(psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT max(version) FROM company.schema_migrations') == 10 ]] || fail 'migration 10 missing'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'pre-migration backup'
grep -Fq 'console.py' "$HOME/.config/systemd/user/oaos-company.service" || fail 'console unit missing'
grep -Fq "OAOS_COMPANY_CONSOLE_PORT=$port" "$HOME/.config/systemd/user/oaos-company.service" || fail 'console port not configured'
[[ -f $HOME/.local/lib/oaos-company/console.py ]] || fail 'console application not installed'
printf 'PASS: C09 migration 10, backup, console unit and application\n'

# --- Real HTTP console -------------------------------------------------------
admin_hash=$(printf 'admin-token' | sha256sum | cut -d' ' -f1)
member_hash=$(printf 'member-token' | sha256sum | cut -d' ' -f1)
csrf_hash=$(printf 'csrf-token' | sha256sum | cut -d' ' -f1)
psql -X -q -v ON_ERROR_STOP=1 -v admin_hash="$admin_hash" \
  -v member_hash="$member_hash" -v csrf_hash="$csrf_hash" >/dev/null <<'SQL'
SELECT set_config('company.org_id', '00000000-0000-0000-0000-000000000001', false);
INSERT INTO company.organizations(id, slug)
  VALUES ('00000000-0000-0000-0000-000000000001', 'c09-http-org');
INSERT INTO company.members(id, org_id, project_user_id, role, state) VALUES
  ('00000000-0000-0000-0000-000000000011', '00000000-0000-0000-0000-000000000001', 'c09-http-admin', 'admin', 'active'),
  ('00000000-0000-0000-0000-000000000012', '00000000-0000-0000-0000-000000000001', 'c09-http-member', 'member', 'active');
SELECT set_config('company.member_id', '00000000-0000-0000-0000-000000000011', false);
INSERT INTO company.console_sessions(org_id, member_id, token_hash, csrf_hash, expires_at) VALUES
  ('00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000011', :'admin_hash', :'csrf_hash', now() + interval '8 hours'),
  ('00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000012', :'member_hash', :'csrf_hash', now() + interval '8 hours');
SQL
python3 "$HOME/.local/lib/oaos-company/console.py" >"$temp_root/console.log" 2>&1 &
console_pid=$!
for _ in $(seq 1 60); do
  curl -sf --noproxy '*' "http://127.0.0.1:$port/company/health" >/dev/null 2>&1 && break
  sleep 0.1
done
[[ $(curl -s --noproxy '*' "http://127.0.0.1:$port/company/health") == ok ]] || fail 'console health'
[[ $(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' "http://127.0.0.1:$port/company/console/api/members") == 401 ]] || fail 'unauthenticated API'
curl -s --noproxy '*' -H 'Cookie: oaos_company_session=admin-token' \
  "http://127.0.0.1:$port/company/console/api/members" > "$temp_root/admin.json"
curl -s --noproxy '*' -H 'Cookie: oaos_company_session=member-token' \
  "http://127.0.0.1:$port/company/console/api/members" > "$temp_root/member.json"
grep -Fq 'c09-http-admin' "$temp_root/admin.json" || fail 'admin member view'
grep -Fq 'c09-http-member' "$temp_root/admin.json" || fail 'admin member view missing member'
grep -Fq '"scope": "organization"' "$temp_root/admin.json" || fail 'admin scope'
grep -Fq '"scope": "self"' "$temp_root/member.json" || fail 'member scope'
grep -Fq 'c09-http-member' "$temp_root/member.json" || fail 'member self view'
! grep -Fq 'c09-http-admin' "$temp_root/member.json" || fail 'member saw another member'
! grep -Fq 'token_hash' "$temp_root/admin.json" "$temp_root/member.json" || fail 'session hash exposed'
[[ $(curl -s -o "$temp_root/admin.html" -w '%{http_code}' --noproxy '*' \
  -H 'Cookie: oaos_company_session=admin-token' "http://127.0.0.1:$port/company/console/members") == 200 ]] || fail 'admin page'
grep -Fq '구성원' "$temp_root/admin.html" || fail 'console navigation'
[[ $(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' \
  -H 'Cookie: oaos_company_session=admin-token' "http://127.0.0.1:$port/company/console/unknown") == 404 ]] || fail 'unknown tab'
logout_headers=$(curl -s -D - -o /dev/null --noproxy '*' -X POST \
  -H 'Cookie: oaos_company_session=admin-token; oaos_company_csrf=csrf-token' \
  --data 'csrf=csrf-token' "http://127.0.0.1:$port/company/console/logout")
grep -Fq 'Secure' <<< "$logout_headers" || fail 'session cookie Secure flag'
grep -Fq 'HttpOnly' <<< "$logout_headers" || fail 'session cookie HttpOnly flag'
grep -Fq 'SameSite=Strict' <<< "$logout_headers" || fail 'session cookie SameSite flag'
[[ $(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' \
  -H 'Cookie: oaos_company_session=admin-token' "http://127.0.0.1:$port/company/console/api/members") == 401 ]] || fail 'logout did not revoke session'
! grep -Fq 'admin-token' "$temp_root/console.log" || fail 'console logged a session token'
printf 'PASS: HTTP health, admin/member scope, masking, CSRF logout and cookie flags\n'

# --- SQL read-back and idempotence ------------------------------------------
bash "$verify" --phase c09 --read-back > "$temp_root/verified" || fail 'C09 read-back'
grep -Fq 'Summary: PASS=8 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/verified" || fail 'C09 summary'
[[ $(python3 - "$HOME/.oaos-install/company-state.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding='utf-8'))['stages']['c09']['status'])
PY
) == verified ]] || fail 'verified checkpoint'
bash "$install" --stage c09 > "$temp_root/rerun" || fail 'C09 idempotent rerun'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'rerun created unnecessary backup'
[[ $(psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT max(version) FROM company.schema_migrations') == 10 ]] || fail 'rerun changed migration version'
bash "$verify" --phase c01 --read-back > "$temp_root/c01" || fail 'C01 read-back after C09'
grep -Fq 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/c01" || fail 'C01 regression summary'
[[ $(psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.organizations WHERE slug LIKE 'c09-a-%' OR slug LIKE 'c09-b-%'") == 0 ]] || fail 'fixture rollback'
printf 'PASS: C09 read-back, verified checkpoint, idempotence and C01 regression\n'
