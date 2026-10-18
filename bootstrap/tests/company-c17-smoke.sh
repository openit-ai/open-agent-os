#!/usr/bin/env bash
# Run with: pg_virtualenv bash -e -o pipefail bootstrap/tests/company-c17-smoke.sh
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
printf 'c17-previous-boot\n' > "$temp_root/home/.config/oaos-company/initial-boot-id"
chmod 600 "$temp_root/home/.config/oaos-company/initial-boot-id"
python3 - "$temp_root/home/.oaos-install/company-state.json" <<'PY'
import json, sys
with open(sys.argv[1], 'w', encoding='utf-8') as out:
    json.dump({'edition': 'company', 'stages': {
        'c01': {'status': 'verified'}, 'c02': {'status': 'verified'},
        'c03': {'status': 'verified'}, 'c04': {'status': 'verified'},
        'c05': {'status': 'verified'}, 'c07': {'status': 'verified'},
        'c10': {'status': 'verified'}, 'c15': {'status': 'verified'}}}, out)
PY
cat > "$temp_root/bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
case ${1:-} in
  --user) case ${2:-} in
    show-environment) [[ ${OAOS_C17_NO_USER_MANAGER:-0} != 1 ]]; exit $? ;;
    list-unit-files) exit 0 ;;
    is-enabled) printf 'enabled\n'; exit 0 ;;
    is-active) printf 'active\n'; exit 0 ;;
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
export OAOS_COMPANY_USER OAOS_COMPANY_E_ID=E-c17-test
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

# C17 requires C10, C15 and C16 in addition to the C02 host gates.
code=0
bash "$repo_root/editions/company/install.sh" --stage c17 > "$temp_root/prereq" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'C10/C15/C16 verified gate'
grep -Fq 'C01, C02, C10, C15 and C16 must be verified' "$temp_root/prereq" || fail 'gate reason'
python3 - "$HOME/.oaos-install/company-state.json" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding='utf-8') as src:
    state = json.load(src)
state['stages']['c16'] = {'status': 'verified'}
with open(path, 'w', encoding='utf-8') as out:
    json.dump(state, out)
PY
code=0
OAOS_C17_NO_USER_MANAGER=1 bash "$repo_root/editions/company/install.sh" --stage c17 > "$temp_root/no-manager" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'systemd user-manager gate'
grep -Fq 'systemd user manager is unavailable' "$temp_root/no-manager" || fail 'systemd gate reason'
code=0
PGPORT=1 bash "$repo_root/editions/company/install.sh" --stage c17 > "$temp_root/no-db" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'database connection gate'
grep -Fq 'E database connection failed' "$temp_root/no-db" || fail 'database gate reason'

psql -X -q -v ON_ERROR_STOP=1 -c 'CREATE ROLE c17_test NOSUPERUSER NOBYPASSRLS' >/dev/null
psql -X -q -v ON_ERROR_STOP=1 -c "GRANT CREATE ON DATABASE \"$PGDATABASE\" TO c17_test" >/dev/null
code=0
env -u PGOPTIONS bash "$repo_root/editions/company/install.sh" --stage c17 > "$temp_root/bypass-role" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'RLS bypass role gate'
grep -Fq 'database role bypasses RLS' "$temp_root/bypass-role" || fail 'RLS bypass gate reason'

export PGOPTIONS='-c role=c17_test'
bash "$repo_root/editions/company/migrate.sh" --through 11 >/dev/null
env -u PGOPTIONS psql -X -q -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
INSERT INTO company.organizations(slug) VALUES ('install-org');
INSERT INTO company.members(org_id, project_user_id, role, state)
  SELECT id, 'c17-install-admin', 'admin', 'active'
    FROM company.organizations WHERE slug = 'install-org';
SQL

# A clean handover without a selected integration records evidence and verifies.
bash "$repo_root/editions/company/install.sh" --stage c17 > "$temp_root/installed" || fail 'C17 install'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT max(version) FROM company.schema_migrations') == 12 ]] || fail 'migration 12 missing'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'pre-migration backup'
[[ $(find "$OAOS_COMPANY_BACKUP_DIR" -name 'company-c17-*.dump' | wc -l) == 1 ]] || fail 'handover backup'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT state FROM company.handover_state") == verified ]] || fail 'handover verdict'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.backup_runs") == 1 ]] || fail 'backup record'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.recovery_drills WHERE state = 'passed'") == 1 ]] || fail 'recovery record'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM pg_database WHERE datname LIKE 'oaos_company_c17_restore_%'") == 0 ]] || fail 'scratch database leak'
! grep -Eq 'password|secret|token' "$temp_root/installed" || fail 'secret leaked to install output'

if ! OAOS_C17_VERIFY_DEBUG=1 bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c17 --read-back > "$temp_root/verified-skip" 2>&1; then
  printf '%s\n' 'C17 read-back output:' >&2
  cat "$temp_root/verified-skip" >&2 || true
  fail 'C17 read-back'
fi
grep -Fq 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=1' "$temp_root/verified-skip" || fail 'C17 SKIP read-back summary'
[[ $(python3 - "$HOME/.oaos-install/company-state.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding='utf-8'))['stages']['c17']['status'])
PY
) == verified ]] || fail 'verified checkpoint'

# A rerun reuses the verified backup and does not duplicate handover evidence.
bash "$repo_root/editions/company/install.sh" --stage c17 > "$temp_root/rerun" || fail 'C17 idempotent rerun'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'rerun created unnecessary backup'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.backup_runs") == 1 ]] || fail 'rerun duplicated backup record'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.recovery_drills") == 1 ]] || fail 'rerun duplicated recovery record'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.handover_state") == 1 ]] || fail 'rerun duplicated handover state'

# A selected integration needs a real round-trip confirmation before handover.
env -u PGOPTIONS psql -X -q -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
INSERT INTO company.integration_settings(org_id, provider, state, reason)
  SELECT id, 'slack', 'selected', 'fixture' FROM company.organizations WHERE slug = 'install-org';
INSERT INTO company.integration_apps(org_id, provider, workspace_id, state)
  SELECT id, 'slack', 'c17-workspace', 'active' FROM company.organizations WHERE slug = 'install-org';
SQL
code=0
bash "$repo_root/editions/company/install.sh" --stage c17 > "$temp_root/roundtrip-missing" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'selected integration round-trip gate'
grep -Fq 'external round-trip evidence is missing for slack' "$temp_root/roundtrip-missing" || fail 'round-trip gate reason'
! grep -Fq 'C17 handover verified' "$temp_root/roundtrip-missing" || fail 'handover passed without round-trip'

OAOS_COMPANY_C17_ROUNDTRIP=slack bash "$repo_root/editions/company/install.sh" --stage c17 > "$temp_root/selected" || fail 'C17 selected round-trip install'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT external_roundtrip->>'slack' FROM company.handover_state") == confirmed ]] || fail 'round-trip evidence'
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c17 --read-back > "$temp_root/verified-selected" || fail 'C17 selected read-back'
grep -Fq 'Summary: PASS=8 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/verified-selected" || fail 'C17 selected read-back summary'

bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c01 --read-back > "$temp_root/c01-regression" || fail 'C01 read-back after migration 12'
grep -Fq 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/c01-regression" || fail 'C01 regression summary'
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c04 --read-back > "$temp_root/c04-regression" || fail 'C04 read-back after migration 12'
grep -Fq 'Summary: PASS=5 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/c04-regression" || fail 'C04 regression summary'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.organizations WHERE slug LIKE 'c17-%'") == 0 ]] || fail 'fixture rollback'
printf 'PASS: C17 gates, migration 12, backup/restore, reboot, SKIP/selected round-trip, revocation, idempotence\n'
