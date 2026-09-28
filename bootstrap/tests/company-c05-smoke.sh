#!/usr/bin/env bash
# Run with: pg_virtualenv bash -e -o pipefail bootstrap/tests/company-c05-smoke.sh
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
[[ -n ${PGPORT:-} ]] || { printf 'Temporary PostgreSQL (pg_virtualenv) is required.\n' >&2; exit 3; }
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
mkdir -p "$temp_root/home/.oaos-install" "$temp_root/home/.config/oaos-company" \
  "$temp_root/bin" "$temp_root/backup" "$temp_root/key-backup" "$temp_root/rollback"
chmod 700 "$temp_root/backup" "$temp_root/key-backup" "$temp_root/rollback"
chmod 700 "$temp_root/home/.config/oaos-company"
printf 'server { listen 443 ssl; }\n' > "$temp_root/vhost.conf"
python3 - "$temp_root/home/.oaos-install/company-state.json" <<'PY'
import json, sys
with open(sys.argv[1], 'w', encoding='utf-8') as out:
    json.dump({'edition': 'company', 'stages': {'c01': {'status': 'verified'}, 'c02': {'status': 'verified'}}}, out)
PY
cat > "$temp_root/bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
case ${1:-} in
  --user) case ${2:-} in
    show-environment) [[ ${OAOS_C05_NO_USER_MANAGER:-0} != 1 ]]; exit $? ;;
    list-unit-files) exit 0 ;;
  esac ;;
  is-enabled) printf 'enabled\n'; exit 0 ;;
  is-active) printf 'active\n'; exit 0 ;;
esac
exit 1
MOCK
cat > "$temp_root/bin/loginctl" <<'MOCK'
#!/usr/bin/env bash
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
export OAOS_COMPANY_USER OAOS_COMPANY_E_ID=E-c05-test
OAOS_COMPANY_USER=$(id -un)
export PGHOST=localhost PGHOSTADDR=127.0.0.1
export OAOS_COMPANY_PG_SOCKET_DIR=/tmp
export OAOS_COMPANY_NGINX_VHOST="$temp_root/vhost.conf"
export OAOS_COMPANY_BACKUP_DIR="$temp_root/backup"
export OAOS_COMPANY_KEY_BACKUP_DIR="$temp_root/key-backup"
export OAOS_COMPANY_ROLLBACK_DIR="$temp_root/rollback"
export OAOS_COMPANY_HTTPS_ORIGIN=https://e.example.test
printf 'OAOS_COMPANY_USER=%s\nOAOS_COMPANY_E_ID=%s\nPGDATABASE=%s\nPGHOST=%s\nPGUSER=%s\nOAOS_COMPANY_HTTPS_ORIGIN=%s\nOAOS_COMPANY_NGINX_VHOST=%s\nOAOS_COMPANY_BACKUP_DIR=%s\nOAOS_COMPANY_KEY_BACKUP_DIR=%s\nOAOS_COMPANY_ROLLBACK_DIR=%s\n' \
  "$OAOS_COMPANY_USER" "$OAOS_COMPANY_E_ID" "$PGDATABASE" "$PGHOST" "$PGUSER" \
  "$OAOS_COMPANY_HTTPS_ORIGIN" "$OAOS_COMPANY_NGINX_VHOST" "$OAOS_COMPANY_BACKUP_DIR" \
  "$OAOS_COMPANY_KEY_BACKUP_DIR" "$OAOS_COMPANY_ROLLBACK_DIR" \
  > "$HOME/.config/oaos-company/deployment.conf"
chmod 600 "$HOME/.config/oaos-company/deployment.conf"
printf 'PGDATABASE=%s\n' "$PGDATABASE" > "$HOME/.config/oaos-company/.env"
chmod 600 "$HOME/.config/oaos-company/.env"

code=0
bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/blocked" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'C04 verified gate'
grep -Fq 'C01, C02, C03 and C04 must be verified' "$temp_root/blocked" || fail 'gate reason'
python3 - "$HOME/.oaos-install/company-state.json" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding='utf-8') as src:
    state = json.load(src)
state['stages']['c03'] = {'status': 'verified'}
state['stages']['c04'] = {'status': 'verified'}
with open(path, 'w', encoding='utf-8') as out:
    json.dump(state, out)
PY
code=0
OAOS_C05_NO_USER_MANAGER=1 bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/no-manager" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'systemd user-manager gate'
grep -Fq 'systemd user manager is unavailable' "$temp_root/no-manager" || fail 'systemd gate reason'
code=0
PGPORT=1 bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/no-db" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'database connection gate'
grep -Fq 'E database connection failed' "$temp_root/no-db" || fail 'database gate reason'

# The temporary cluster authenticates its creator; role startup options make
# all Company SQL run as a separate non-superuser without a password argument.
psql -X -q -v ON_ERROR_STOP=1 -c 'CREATE ROLE c05_test NOSUPERUSER NOBYPASSRLS' >/dev/null
psql -X -q -v ON_ERROR_STOP=1 -c "GRANT CREATE ON DATABASE \"$PGDATABASE\" TO c05_test" >/dev/null
code=0
env -u PGOPTIONS bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/bypass-role" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'RLS bypass role gate'
grep -Fq 'database role bypasses RLS' "$temp_root/bypass-role" || fail 'RLS bypass gate reason'
export PGOPTIONS='-c role=c05_test'
bash "$repo_root/editions/company/migrate.sh" --through 3 >/dev/null

bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/installed" || fail 'C05 install'
[[ $(psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT max(version) FROM company.schema_migrations') == 4 ]] || fail 'migration 4 missing'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'pre-migration backup'
[[ $(find "$OAOS_COMPANY_KEY_BACKUP_DIR" -name company.env | wc -l) == 1 ]] || fail 'restricted key backup'
bash -e -o pipefail -c 'python3 "$1" check-key "$2"' _ "$repo_root/editions/company/vault.py" "$HOME/.config/oaos-company/.env" || fail 'AES-GCM key presence'
mv "$HOME/.config/oaos-company/.env" "$HOME/.config/oaos-company/.env.saved"
printf 'PGDATABASE=%s\n' "$PGDATABASE" > "$HOME/.config/oaos-company/.env"
chmod 600 "$HOME/.config/oaos-company/.env"
code=0
bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/missing-key" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'applied migration must not regenerate a missing key'
mv "$HOME/.config/oaos-company/.env.saved" "$HOME/.config/oaos-company/.env"
bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/restored-key" || fail 'restored key rerun'
[[ $(find "$OAOS_COMPANY_KEY_BACKUP_DIR" -name company.env | wc -l) == 1 ]] || fail 'missing key gate created backup'
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c01 --read-back > "$temp_root/c01-regression" || fail 'C01 schema read-back after C05'
grep -Fq 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/c01-regression" || fail 'C01 regression summary'
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c04 --read-back > "$temp_root/c04-regression" || fail 'C04 assistant read-back after C05'
grep -Fq 'Summary: PASS=5 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/c04-regression" || fail 'C04 regression summary'
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c05 --read-back > "$temp_root/verified" || fail 'C05 real DB read-back'
grep -Fq 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/verified" || fail 'read-back summary'
grep '^Summary:' "$temp_root/verified"
[[ $(psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.organizations WHERE slug LIKE 'c05-%'") == 0 ]] || fail 'fixture rollback'
[[ $(python3 - "$HOME/.oaos-install/company-state.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding='utf-8'))['stages']['c05']['status'])
PY
) == verified ]] || fail 'verified checkpoint'
bash "$repo_root/editions/company/install.sh" --stage c05 > "$temp_root/rerun" || fail 'C05 idempotent rerun'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'rerun created unnecessary backup'
[[ $(find "$OAOS_COMPANY_KEY_BACKUP_DIR" -name company.env | wc -l) == 1 ]] || fail 'rerun created unnecessary key backup'
printf 'PASS: C05 gates, migration 4, backups, SQL responses, rollback, idempotence\n'
printf 'PASS: C01 and C04 read-back remain compatible after migration 4\n'

export OAOS_C05_REPO_ROOT="$repo_root"
python3 - <<'PY'
import os
import sys
sys.path.insert(0, os.path.join(os.environ['OAOS_C05_REPO_ROOT'], 'editions', 'company'))
from vault import VaultAccessDenied, open_envelope, seal
key = os.urandom(32)
value = os.urandom(40)
first, first_nonce = seal(value, key, 'org', 'owner-a', 'provider', 'purpose')
assert open_envelope(first, first_nonce, key, 'org', 'owner-a', 'provider', 'purpose') == value
second, second_nonce = seal(os.urandom(40), key, 'org', 'owner-a', 'provider', 'purpose')
assert first != second and first_nonce != second_nonce
for envelope, nonce, owner in ((first, first_nonce, 'owner-b'), (first, second_nonce, 'owner-a')):
    try:
        open_envelope(envelope, nonce, key, 'org', owner, 'provider', 'purpose')
    except VaultAccessDenied:
        pass
    else:
        raise AssertionError('vault binding was not authenticated')
PY
printf 'PASS: AES-256-GCM round trip, owner binding and nonce authentication\n'

# A function drift must fail the same real query path even if the file checksum remains valid.
psql -X -q -v ON_ERROR_STOP=1 -c \
  "CREATE OR REPLACE FUNCTION company.personal_secret_use_decision(p_secret uuid, p_assistant uuid, p_generation integer) RETURNS text LANGUAGE sql STABLE SECURITY INVOKER AS 'SELECT ''allow''::text'" >/dev/null
code=0
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c05 --read-back > "$temp_root/drift" 2>&1 || code=$?
[[ $code == 1 ]] || fail 'read-back accepted broken authorization function'
printf 'PASS: real query read-back rejects changed authorization behavior\n'
