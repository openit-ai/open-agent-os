#!/usr/bin/env bash
# Run with: pg_virtualenv bash -e -o pipefail bootstrap/tests/company-c10-smoke.sh
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
[[ -n ${PGPORT:-} ]] || { printf 'Temporary PostgreSQL (pg_virtualenv) is required.\n' >&2; exit 3; }
temp_root=$(mktemp -d)
api_pid=''
app_pid=''
cleanup() {
  [[ -z $app_pid ]] || kill "$app_pid" 2>/dev/null || true
  [[ -z $api_pid ]] || kill "$api_pid" 2>/dev/null || true
  wait 2>/dev/null || true
  rm -rf -- "$temp_root"
}
trap cleanup EXIT
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
mkdir -p "$temp_root/home/.oaos-install" "$temp_root/home/.config/oaos-company" \
  "$temp_root/bin" "$temp_root/backup" "$temp_root/key-backup" "$temp_root/rollback"
chmod 700 "$temp_root/backup" "$temp_root/key-backup" "$temp_root/rollback"
chmod 700 "$temp_root/home/.config/oaos-company"
printf 'server { listen 443 ssl; }\n' > "$temp_root/vhost.conf"
python3 - "$temp_root/home/.oaos-install/company-state.json" <<'PY'
import json, sys
with open(sys.argv[1], 'w', encoding='utf-8') as out:
    json.dump({'edition': 'company', 'stages': {
        'c01': {'status': 'verified'}}}, out)
PY
cat > "$temp_root/bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
case ${1:-} in
  --user) case ${2:-} in
    show-environment) [[ ${OAOS_C10_NO_USER_MANAGER:-0} != 1 ]]; exit $? ;;
    daemon-reload|restart|start) exit 0 ;;
    list-unit-files) exit 0 ;;
    is-enabled) printf 'enabled\n'; exit 0 ;;
    is-active) printf 'active\n'; exit 0 ;;
    show) printf 'success\n'; exit 0 ;;
  esac ;;
  is-enabled) printf 'enabled\n'; exit 0 ;;
  is-active) printf 'active\n'; exit 0 ;;
esac
exit 1
MOCK
cat > "$temp_root/bin/loginctl" <<'MOCK'
#!/usr/bin/env bash
printf 'yes\n'
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
export OAOS_COMPANY_USER OAOS_COMPANY_E_ID=E-c10-test
OAOS_COMPANY_USER=$(id -un)
export PGHOST=localhost PGHOSTADDR=127.0.0.1
export OAOS_COMPANY_PG_SOCKET_DIR=/tmp
export OAOS_COMPANY_NGINX_VHOST="$temp_root/vhost.conf"
export OAOS_COMPANY_BACKUP_DIR="$temp_root/backup"
export OAOS_COMPANY_KEY_BACKUP_DIR="$temp_root/key-backup"
export OAOS_COMPANY_ROLLBACK_DIR="$temp_root/rollback"
export OAOS_COMPANY_HTTPS_ORIGIN=https://e.example.test
export OAOS_COMPANY_ORG_SLUG=company
export OAOS_COMPANY_COOKIE_SECURE=0
printf 'OAOS_COMPANY_USER=%s\nOAOS_COMPANY_E_ID=%s\nPGDATABASE=%s\nPGHOST=%s\nPGUSER=%s\nOAOS_COMPANY_HTTPS_ORIGIN=%s\nOAOS_COMPANY_NGINX_VHOST=%s\nOAOS_COMPANY_BACKUP_DIR=%s\nOAOS_COMPANY_KEY_BACKUP_DIR=%s\nOAOS_COMPANY_ROLLBACK_DIR=%s\n' \
  "$OAOS_COMPANY_USER" "$OAOS_COMPANY_E_ID" "$PGDATABASE" "$PGHOST" "$PGUSER" \
  "$OAOS_COMPANY_HTTPS_ORIGIN" "$OAOS_COMPANY_NGINX_VHOST" "$OAOS_COMPANY_BACKUP_DIR" \
  "$OAOS_COMPANY_KEY_BACKUP_DIR" "$OAOS_COMPANY_ROLLBACK_DIR" \
  > "$HOME/.config/oaos-company/deployment.conf"
chmod 600 "$HOME/.config/oaos-company/deployment.conf"

api_port=18765
cat > "$temp_root/mock_project_api.py" <<'PY'
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ADMIN = os.environ['MOCK_ADMIN']
LOG = os.environ['MOCK_DM_LOG']


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.path.startswith('/users/'):
            self._json(404, {'error': 'not found'})
            return
        user = self.path[len('/users/'):]
        if user not in (ADMIN, 'project-member-1', 'project-member-2'):
            self._json(404, {'error': 'not found'})
            return
        self._json(200, {'id': user, 'delete_at': 0,
                         'roles': 'system_admin' if user == ADMIN else 'system_user'})

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        payload = json.loads(self.rfile.read(length) or b'{}')
        if self.path == '/channels/direct':
            self._json(200, {'id': 'dm-' + '-'.join(payload)})
        elif self.path == '/posts':
            with open(LOG, 'a', encoding='utf-8') as out:
                out.write(json.dumps(payload) + '\n')
            self._json(200, {'id': 'post-1'})
        else:
            self._json(404, {'error': 'not found'})


port = int(sys.argv[1])
ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
PY
export MOCK_ADMIN=project-admin MOCK_DM_LOG="$temp_root/dm.log"
python3 "$temp_root/mock_project_api.py" "$api_port" > "$temp_root/api.log" 2>&1 &
api_pid=$!
export OAOS_COMPANY_PROJECT_API_URL="http://127.0.0.1:$api_port"
export OAOS_COMPANY_PROJECT_API_TOKEN=test-project-token-0123456789
export OAOS_COMPANY_PROJECT_BOT_USER_ID=company-bot
export OAOS_COMPANY_PROJECT_ADMIN_ROLES=system_admin
export OAOS_COMPANY_CONSOLE_SECRET
OAOS_COMPANY_CONSOLE_SECRET=$(python3 -c 'import base64,os; print(base64.b64encode(os.urandom(32)).decode("ascii"))')
printf 'PGDATABASE=%s\nOAOS_COMPANY_CONSOLE_SECRET=%s\n' "$PGDATABASE" "$OAOS_COMPANY_CONSOLE_SECRET" \
  > "$HOME/.config/oaos-company/.env"
chmod 600 "$HOME/.config/oaos-company/.env"

# Start the console from the working tree so the installer's health read-back
# sees the real HTTP routes before the unit is replaced.
python3 "$repo_root/editions/company/console_app.py" > "$temp_root/app.log" 2>&1 &
app_pid=$!
for _ in $(seq 1 50); do
  [[ $(curl --silent --max-time 1 http://127.0.0.1:8765/company/health 2>/dev/null || true) == ok ]] && break
  sleep 0.1
done
[[ $(curl --silent --max-time 1 http://127.0.0.1:8765/company/health 2>/dev/null) == ok ]] || fail 'console health start'

code=0
bash "$repo_root/editions/company/install.sh" --stage c10 > "$temp_root/blocked" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'C02/C03 verified gate'
grep -Fq 'C02 and C03 must be verified' "$temp_root/blocked" || fail 'gate reason'
python3 - "$HOME/.oaos-install/company-state.json" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding='utf-8') as src:
    state = json.load(src)
state['stages']['c02'] = {'status': 'verified'}
state['stages']['c03'] = {'status': 'verified'}
with open(path, 'w', encoding='utf-8') as out:
    json.dump(state, out)
PY
code=0
OAOS_C10_NO_USER_MANAGER=1 bash "$repo_root/editions/company/install.sh" --stage c10 > "$temp_root/no-manager" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'systemd user-manager gate'
grep -Fq 'systemd user manager is unavailable' "$temp_root/no-manager" || fail 'systemd gate reason'
code=0
PGPORT=1 bash "$repo_root/editions/company/install.sh" --stage c10 > "$temp_root/no-db" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'database connection gate'
grep -Fq 'E database connection failed' "$temp_root/no-db" || fail 'database gate reason'
psql -X -q -v ON_ERROR_STOP=1 -c 'CREATE ROLE c10_test NOSUPERUSER NOBYPASSRLS' >/dev/null
psql -X -q -v ON_ERROR_STOP=1 -c "GRANT CREATE ON DATABASE \"$PGDATABASE\" TO c10_test" >/dev/null
export PGOPTIONS='-c role=c10_test'
code=0
env -u OAOS_COMPANY_PROJECT_API_TOKEN bash "$repo_root/editions/company/install.sh" --stage c10 > "$temp_root/no-api" 2>&1 || code=$?
[[ $code == 3 ]] || { cat "$temp_root/no-api" >&2; fail 'Project API gate'; }
grep -Fq 'Project API URL, token, bot user ID' "$temp_root/no-api" || { cat "$temp_root/no-api" >&2; fail 'Project API gate reason'; }

bash "$repo_root/editions/company/install.sh" --stage c10 > "$temp_root/installed" || fail 'C10 install'
[[ $(psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT max(version) FROM company.schema_migrations') == 11 ]] || fail 'migration 11 missing'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'pre-migration backup'
[[ -f $HOME/.config/oaos-company/console.conf ]] || fail 'setup link file'
[[ $(stat -c %a "$HOME/.config/oaos-company/console.conf") == 600 ]] || fail 'setup link mode'
grep -Fq 'OAOS_COMPANY_SETUP_URL=https://e.example.test/company/setup' "$HOME/.config/oaos-company/console.conf" || fail 'setup URL'
grep -Fq 'ExecStart=/usr/bin/python3 ' "$HOME/.config/systemd/user/oaos-company.service" || fail 'unit ExecStart'
grep -Fq 'console_app.py' "$HOME/.config/systemd/user/oaos-company.service" || fail 'unit console'
[[ $(stat -c %a "$HOME/.config/oaos-company/.env") == 600 ]] || fail 'env mode'

bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c10 --read-back > "$temp_root/verified" 2>&1 || { cat "$temp_root/verified" >&2; fail 'C10 real DB read-back'; }
grep -Fq 'Summary: PASS=8 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/verified" || fail 'read-back summary'
grep '^Summary:' "$temp_root/verified"
[[ $(psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.organizations WHERE slug LIKE 'c10-%'") == 0 ]] || fail 'fixture rollback'
[[ $(python3 - "$HOME/.oaos-install/company-state.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding='utf-8'))['stages']['c10']['status'])
PY
) == verified ]] || fail 'verified checkpoint'
bash "$repo_root/editions/company/install.sh" --stage c10 > "$temp_root/rerun" || fail 'C10 idempotent rerun'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'rerun created unnecessary backup'
printf 'PASS: C10 gates, migration 11, console install, G10 read-back, rollback, idempotence\n'

# Browser flow against the real console routes and a Project API mock.
base=http://127.0.0.1:8765
setup_page="$temp_root/setup.html"
[[ $(curl --silent --output "$setup_page" --write-out '%{http_code}' "$base/company/setup") == 200 ]] || fail 'setup page'
grep -Fq 'G10 초기 관리자 설정' "$setup_page" || fail 'setup page content'
status=$(curl --silent --output "$temp_root/start.html" --write-out '%{http_code}' \
  --data-urlencode 'project_user_id=project-admin' "$base/company/setup/start")
[[ $status == 200 ]] || fail 'setup start'
sleep 0.1
admin_code=$(sed -n 's/.*: \([A-F0-9]\{8\}\) (10.*/\1/p' "$temp_root/dm.log" | tail -1)
[[ -n $admin_code ]] || fail 'admin code delivery'
admin_jar="$temp_root/admin.jar"
status=$(curl --silent --output "$temp_root/confirm.html" --write-out '%{http_code}' \
  -c "$admin_jar" --data-urlencode 'project_user_id=project-admin' \
  --data-urlencode "code=$admin_code" "$base/company/setup/confirm")
[[ $status == 303 ]] || fail 'admin confirm redirect'
curl --silent -b "$admin_jar" "$base/company/setup" > "$temp_root/mappings.html"
csrf=$(python3 - "$temp_root/mappings.html" <<'CSRF'
import re, sys
text = open(sys.argv[1], encoding='utf-8').read()
match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', text)
print(match.group(1) if match else '')
CSRF
)
[[ -n $csrf ]] || fail 'mapping CSRF token'
status=$(curl --silent --output "$temp_root/mapped.html" --write-out '%{http_code}' \
  -b "$admin_jar" --data-urlencode 'csrf_token=wrong' \
  --data-urlencode $'project_user_ids=project-member-1\nproject-member-2' \
  "$base/company/setup/mappings")
[[ $status == 403 ]] || fail 'mapping CSRF rejection'
status=$(curl --silent --output "$temp_root/mapped.html" --write-out '%{http_code}' \
  -b "$admin_jar" --data-urlencode "csrf_token=$csrf" \
  --data-urlencode $'project_user_ids=project-member-1\nproject-member-2' \
  "$base/company/setup/mappings")
[[ $status == 200 ]] || fail 'mapping save'
grep -Fq 'G10 완료' "$temp_root/mapped.html" || fail 'G10 completion'
curl --silent -b "$admin_jar" "$base/company/console" > "$temp_root/admin-console.html"
grep -Fq 'project-member-1' "$temp_root/admin-console.html" || fail 'admin sees member one'
grep -Fq 'project-member-2' "$temp_root/admin-console.html" || fail 'admin sees member two'
grep -Fq '연동' "$temp_root/admin-console.html" || fail 'console integration tab'

[[ $(curl --silent --output /dev/null --write-out '%{http_code}' \
  --data-urlencode 'project_user_id=project-member-1' "$base/company/login/start") == 200 ]] || fail 'member login start'
sleep 0.1
member_code=$(sed -n 's/.*: \([A-F0-9]\{8\}\) (10.*/\1/p' "$temp_root/dm.log" | tail -1)
[[ -n $member_code ]] || fail 'member code delivery'
member_jar="$temp_root/member.jar"
status=$(curl --silent --output /dev/null --write-out '%{http_code}' \
  -c "$member_jar" --data-urlencode 'project_user_id=project-member-1' \
  --data-urlencode "code=$member_code" "$base/company/login/confirm")
[[ $status == 303 ]] || fail 'member login redirect'
curl --silent -b "$member_jar" "$base/company/console" > "$temp_root/member-console.html"
grep -Fq 'project-member-1' "$temp_root/member-console.html" || fail 'member sees self'
if grep -Fq 'project-member-2' "$temp_root/member-console.html"; then fail 'member saw another member'; fi
grep -Fq '본인 비서·연결 상태만' "$temp_root/member-console.html" || fail 'member scope notice'
status=$(curl --silent --output /dev/null --write-out '%{http_code}' \
  -b "$member_jar" --data-urlencode 'csrf_token=wrong' "$base/company/logout")
[[ $status == 303 ]] || fail 'logout CSRF redirect keeps session'

printf 'PASS: HTTPS setup page, one-time admin code, two mappings, login and member scope\n'
