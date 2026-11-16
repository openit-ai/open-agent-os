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
    list-unit-files|daemon-reload|enable) exit 0 ;;
    is-enabled) printf 'enabled\n'; exit 0 ;;
    is-active) printf 'active\n'; exit 0 ;;
    start)
      # Emulate systemd: only the unit's EnvironmentFile reaches ExecStart.
      unit="$HOME/.config/systemd/user/$3"
      env_file=$(sed -n 's/^EnvironmentFile=//p' "$unit")
      exec_start=$(sed -n 's/^ExecStart=//p' "$unit")
      mapfile -t vars < <(grep -E '^[A-Z_]+=' "$env_file")
      env -i HOME="$HOME" PATH="$PATH" "${vars[@]}" $exec_start; exit $? ;;
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
printf 'PGDATABASE=%s\nOAOS_COMPANY_PG_SOCKET_DIR=/tmp\nPGPORT=%s\nOAOS_COMPANY_BACKUP_DIR=%s\nOAOS_COMPANY_KEY_BACKUP_DIR=%s\n' \
  "$PGDATABASE" "$PGPORT" "$OAOS_COMPANY_BACKUP_DIR" "$OAOS_COMPANY_KEY_BACKUP_DIR" > "$HOME/.config/oaos-company/.env"
chmod 600 "$HOME/.config/oaos-company/.env"

# C17 requires C10, C15 and C16 in addition to the C02 host gates.
code=0
bash "$repo_root/editions/company/install.sh" --stage c17 > "$temp_root/prereq" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'C10/C15/C16 verified gate'
grep -Fq 'C01, C02, C10 and C15 must be verified and C16 verified or SKIP' "$temp_root/prereq" || fail 'gate reason'
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
bash "$repo_root/editions/company/lib/migrate.sh" --through 11 >/dev/null
env -u PGOPTIONS psql -X -q -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
INSERT INTO company.organizations(slug) VALUES ('install-org');
INSERT INTO company.members(org_id, project_user_id, role, state)
  SELECT id, 'c17-install-admin', 'admin', 'active'
    FROM company.organizations WHERE slug = 'install-org';
SQL

# A clean handover without a selected integration records evidence and verifies.
bash "$repo_root/editions/company/install.sh" --stage c17 > "$temp_root/installed" || fail 'C17 install'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT max(version) FROM company.schema_migrations WHERE version < 13') == 12 ]] || fail 'migration 12 missing'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'pre-migration backup'
[[ $(find "$OAOS_COMPANY_BACKUP_DIR" -name 'company-c17-*.dump' | wc -l) == 1 ]] || fail 'handover backup'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT state FROM company.handover_state") == verified ]] || fail 'handover verdict'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.backup_runs") == 1 ]] || fail 'backup record'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.recovery_drills WHERE state = 'passed'") == 1 ]] || fail 'recovery record'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM pg_database WHERE datname LIKE 'oaos_company_c17_restore_%'") == 0 ]] || fail 'scratch database leak'
! grep -Eq 'password|secret|token' "$temp_root/installed" || fail 'secret leaked to install output'

# Without a daily backup in the last 24 hours the handover read-back fails.
code=0
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c17 --read-back > "$temp_root/no-daily" 2>&1 || code=$?
[[ $code == 1 ]] && grep -Fq 'Daily backup within 24h' "$temp_root/no-daily" || fail 'missing daily backup accepted'
# The old app-role dump cannot read FORCE RLS tables; the daily job's
# maintenance-account dump can (real pg_dump, real RLS).
if pg_dump --format=custom --dbname="$PGDATABASE" > "$temp_root/app-role.dump" 2>/dev/null; then
  fail 'runtime-role pg_dump unexpectedly bypassed FORCE RLS'
fi
cp "$repo_root/editions/company/app/backup.sh" "$temp_root/backup.sh"
env -i HOME="$HOME" PATH="$PATH" PGDATABASE="$PGDATABASE" PGPORT="$PGPORT" \
  OAOS_COMPANY_PG_SOCKET_DIR=/tmp OAOS_COMPANY_BACKUP_DIR="$OAOS_COMPANY_BACKUP_DIR" \
  OAOS_COMPANY_KEY_BACKUP_DIR="$OAOS_COMPANY_KEY_BACKUP_DIR" bash "$temp_root/backup.sh" || fail 'daily backup job under FORCE RLS'
latest=$(< "$OAOS_COMPANY_BACKUP_DIR/last-success")
pg_restore --list "$OAOS_COMPANY_BACKUP_DIR/company-$latest.dump" | grep -F 'audit_events' >/dev/null || fail 'daily dump lacks Company tables'
[[ $(stat -c %a "$OAOS_COMPANY_BACKUP_DIR/company-$latest.dump") == 600 ]] || fail 'daily dump mode'

if ! OAOS_C17_VERIFY_DEBUG=1 bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c17 --read-back > "$temp_root/verified-skip" 2>&1; then
  printf '%s\n' 'C17 read-back output:' >&2
  cat "$temp_root/verified-skip" >&2 || true
  fail 'C17 read-back'
fi
grep -Fq 'Summary: PASS=9 FAIL=0 MANUAL=0 SKIP=1' "$temp_root/verified-skip" || fail 'C17 SKIP read-back summary'
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
grep -Fq 'Summary: PASS=10 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/verified-selected" || fail 'C17 selected read-back summary'

bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c01 --read-back > "$temp_root/c01-regression" || fail 'C01 read-back after migration 12'
grep -Fq 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/c01-regression" || fail 'C01 regression summary'
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c04 --read-back > "$temp_root/c04-regression" || fail 'C04 read-back after migration 12'
grep -Fq 'Summary: PASS=5 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/c04-regression" || fail 'C04 regression summary'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.organizations WHERE slug LIKE 'c17-%'") == 0 ]] || fail 'fixture rollback'
printf 'PASS: C17 gates, migration 12, backup/restore, reboot, SKIP/selected round-trip, revocation, idempotence\n'

# Retention: 180-day audit rows and 30-day departed personal content are purged
# by the maintenance job only, which records its own audit row.
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.audit_events WHERE event_type = 'maintenance' AND action = 'retention_purged'") -ge 1 ]] || fail 'first retention run evidence'
env -u PGOPTIONS psql -X -q -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
INSERT INTO company.audit_events(org_id, at, event_type, action, decision, request_id)
  SELECT id, now() - interval '200 days', 'fixture', 'old_event', 'allow', 'c17-old'
    FROM company.organizations WHERE slug = 'install-org';
INSERT INTO company.audit_events(org_id, at, event_type, action, decision, request_id)
  SELECT id, now() - interval '100 days', 'fixture', 'kept_event', 'allow', 'c17-kept'
    FROM company.organizations WHERE slug = 'install-org';
INSERT INTO company.members(org_id, project_user_id, role, state, updated_at)
  SELECT id, 'c17-gone', 'member', 'departed', now() - interval '40 days'
    FROM company.organizations WHERE slug = 'install-org';
SELECT set_config('company.member_id', (SELECT id::text FROM company.members WHERE project_user_id = 'c17-gone'), false);
INSERT INTO company.documents(org_id, source, source_id, owner_id, source_acl_version, acl_state)
  SELECT org_id, 'personal', 'c17-gone-doc', id, 'owner', 'known'
    FROM company.members WHERE project_user_id = 'c17-gone';
SQL
if psql -X -q -v ON_ERROR_STOP=1 -c 'SELECT company.maintenance_purge()' >/dev/null 2>&1; then fail 'runtime role ran the retention purge'; fi
env -i HOME="$HOME" PATH="$PATH" PGDATABASE="$PGDATABASE" PGPORT="$PGPORT" OAOS_COMPANY_PG_SOCKET_DIR=/tmp \
  bash "$repo_root/editions/company/app/maintenance.sh" || fail 'maintenance job'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.audit_events WHERE request_id IN ('c17-old', 'c17-kept')") == 1 ]] || fail '180-day audit purge'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.documents WHERE source_id = 'c17-gone-doc'") == 0 ]] || fail '30-day departed content purge'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT (metadata->>'audit_deleted')::int || '|' || (metadata->>'documents_deleted')::int FROM company.audit_events WHERE action = 'retention_purged' ORDER BY at DESC LIMIT 1") == '1|1' ]] || fail 'purge audit row'
printf 'PASS: daily backup under FORCE RLS, 24h backup age, maintenance-only 180/30-day retention\n'
