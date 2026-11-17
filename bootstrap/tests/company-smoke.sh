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
runner="$repo_root/editions/company/lib/migrate.sh"
mkdir -p "$temp_root/home/.oaos-install" "$temp_root/bin"

HOME="$temp_root/home" bash "$install" --help | grep -F 'c01 through c17' >/dev/null || fail 'install help'
env -u HOME bash "$install" --help > /dev/null || fail 'install help without HOME'
HOME="$temp_root/home" bash "$install" --status > "$temp_root/status"
[[ $(grep -Ec '^c[0-9][0-9] +pending ' "$temp_root/status") == 17 ]] || fail 'empty status'
[[ ! -e $temp_root/home/.oaos-install/company-state.json ]] || fail 'status changed state'
HOME="$temp_root/home" bash "$verify" --help | grep -F -- '--read-back' >/dev/null || fail 'verify help'
env -u HOME bash "$verify" --help > /dev/null || fail 'verify help without HOME'
code=0
HOME="$temp_root/home" bash "$install" --stage c03 > /dev/null 2>&1 || code=$?
[[ $code == 3 ]] || fail 'C03 prerequisite gate exit code'
code=0
HOME="$temp_root/home" bash "$install" --stage c01 > /dev/null 2>&1 || code=$?
[[ $code == 3 ]] || fail 'missing Project prerequisite exit code'
HOME="$temp_root/home" bash "$install" --status > "$temp_root/blocked-status"
grep -Eq '^c01 +blocked ' "$temp_root/blocked-status" || fail 'missing Project checkpoint'
code=0
HOME="$temp_root/home" bash "$verify" --phase c01 --read-back > /dev/null 2>&1 || code=$?
[[ $code == 3 ]] || fail 'unapplied verifier exit code'
printf 'PASS: help, read-only status and prerequisite gate\n'

# C01 reads Project readiness from the edition-scoped layout and the older flat
# layout; a Project verify that passed is "verified" (older installers: "done"),
# and MANUAL-pending ("applied") is not ready.
project_ready_case() {
  local layout=$1 verify=$2 expected=$3 code=0
  python3 - "$temp_root/home/.oaos-install/state.json" "$layout" "$verify" <<'PY'
import json, sys
path, layout, verify = sys.argv[1:4]
stages = {'stack': {'status': 'done'}, 'verify': {'status': verify}}
if layout == 'scoped':
    state = {'edition': 'personal', 'stages': {}, 'editions': {'personal': {'stages': {}}, 'project': {'stages': stages}}}
else:
    state = {'edition': 'project', 'stages': stages}
with open(path, 'w', encoding='utf-8') as f:
    json.dump(state, f)
PY
  HOME="$temp_root/home" OAOS_NO_LOG_FILE=1 bash -c '. "$1/bootstrap/lib/common.sh"; . "$1/editions/company/stages/c01.sh"; company_c01_project_ready' _ "$repo_root" > /dev/null 2>&1 || code=$?
  [[ $code == "$expected" ]] || fail "Project readiness $layout/$verify returned $code"
}
project_ready_case scoped verified 0
project_ready_case scoped 'done' 0
project_ready_case scoped applied 3
project_ready_case flat verified 0
project_ready_case flat 'done' 0
printf 'PASS: C01 reads edition-scoped and flat Project state; MANUAL-pending is not ready\n'

python3 - "$temp_root/home/.oaos-install/state.json" <<'PY'
import json, sys
with open(sys.argv[1], 'w', encoding='utf-8') as f:
    json.dump({'edition': 'project', 'stages': {'stack': {'status': 'done'}, 'verify': {'status': 'done'}}}, f)
PY
cat > "$temp_root/bin/psql" <<'MOCK'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${OAOS_FAKE_DB_FAIL:-0} != 1 ]] || exit 2
if [[ $* != *' -c '* && $* != *' -f '* && $* != *--single-transaction* ]]; then
  # migrate.sh advisory lock connection: statements arrive on stdin.
  while IFS= read -r line; do [[ $line == '\echo locked' ]] && printf 'locked\n'; done
  exit 0
elif [[ $* == *--single-transaction* ]]; then
  if [[ $* == *'001_company_schema.sql'* ]]; then version=1
  elif [[ $* == *'002_member_mapping.sql'* ]]; then version=2
  else exit 2; fi
  [[ $* =~ ([0-9a-f]{64}) ]] || exit 2
  printf '%s:%s\n' "$version" "${BASH_REMATCH[1]}" >> "$OAOS_FAKE_DB_STATE"
  printf 'apply\n' >> "$OAOS_FAKE_DB_LOG"
elif [[ $* == *'SELECT to_regclass'* ]]; then
  [[ -f $OAOS_FAKE_DB_STATE ]] && printf 't\n' || printf 'f\n'
elif [[ $* == *'SELECT checksum'* ]]; then
  [[ $* =~ version\ =\ ([0-9]+) ]] || exit 2
  grep "^${BASH_REMATCH[1]}:" "$OAOS_FAKE_DB_STATE" | cut -d: -f2 || true
elif [[ $* == *'SELECT COALESCE(MAX(version)'* ]]; then
  count=$(wc -l < "$OAOS_FAKE_DB_STATE")
  if [[ $* == *'WHERE version <= 1'* ]]; then count=1; fi
  if [[ $* == *'COUNT(*)'* ]]; then printf '%s:%s\n' "$count" "$count"; else printf '%s\n' "$count"; fi
elif [[ $* == *'SELECT 1'* ]]; then
  printf '1\n'
else
  exit 2
fi
MOCK
cat > "$temp_root/bin/sudo" <<'MOCK'
#!/usr/bin/env bash
[[ ${1:-} == -n ]] || exit 1
shift
"$@"
MOCK
cat > "$temp_root/bin/runuser" <<'MOCK'
#!/usr/bin/env bash
[[ ${1:-} == -u && ${2:-} == postgres && ${3:-} == -- ]] || exit 1
shift 3
exec "$@"
MOCK
cat > "$temp_root/bin/pg_dump" <<'MOCK'
#!/usr/bin/env bash
[[ ${OAOS_FAKE_DB_FAIL:-0} != 1 ]] || exit 2
printf 'mock custom dump\n'
MOCK
chmod 700 "$temp_root/bin/psql" "$temp_root/bin/sudo" "$temp_root/bin/runuser" "$temp_root/bin/pg_dump"
mkdir -p "$temp_root/rollback" && chmod 700 "$temp_root/rollback"
export OAOS_COMPANY_ROLLBACK_DIR="$temp_root/rollback" OAOS_COMPANY_PG_SOCKET_DIR=/tmp
export OAOS_FAKE_DB_STATE="$temp_root/db-checksum" OAOS_FAKE_DB_LOG="$temp_root/db-log"
code=0
HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" PGDATABASE=company_test OAOS_COMPANY_ROLLBACK_DIR='' bash "$install" --stage c01 > /dev/null 2>&1 || code=$?
[[ $code == 3 ]] || fail 'C01 without a rollback directory must block'
HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" PGDATABASE=company_test bash "$install" --stage c01 > "$temp_root/first"
[[ $(wc -l < "$OAOS_FAKE_DB_LOG") == 1 ]] || fail 'only C01 migration should be applied'
[[ $(find "$temp_root/rollback" -name company.dump | wc -l) == 1 ]] || fail 'C01 pre-migration backup'
[[ -n $(find "$temp_root/home/.oaos/logs" -name 'company-*.log' 2>/dev/null) &&
   -z $(find "$temp_root/home/.oaos/logs" -name 'install-*.log' 2>/dev/null) ]] || fail 'Company log name'
python3 - "$temp_root/home/.oaos-install/company-state.json" <<'PY' || fail 'applied state'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    state = json.load(f)
assert state['edition'] == 'company'
assert state['stages']['c01']['status'] == 'applied'
assert len(state['stages']) == 17
PY
HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" PGDATABASE=company_test bash "$install" --stage c01 > "$temp_root/second"
[[ $(wc -l < "$OAOS_FAKE_DB_LOG") == 1 ]] || fail 'migration was applied twice'
[[ $(find "$temp_root/rollback" -name company.dump | wc -l) == 1 ]] || fail 'rerun created an unnecessary C01 backup'
HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" PGDATABASE=company_test bash "$runner" --check --through 1 > /dev/null || fail 'checksum check'
printf 'PASS: atomic runner call, checksum skip and applied checkpoint\n'

sed -i "1s/:[0-9a-f]\{64\}$/:$(printf '%064d' 0)/" "$OAOS_FAKE_DB_STATE"
code=0
HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" PGDATABASE=company_test bash "$runner" --check --through 1 > /dev/null 2>&1 || code=$?
[[ $code == 1 ]] || fail 'checksum drift exit code'
code=0
OAOS_FAKE_DB_FAIL=1 HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" PGDATABASE=company_test bash "$install" --stage c01 > /dev/null 2>&1 || code=$?
[[ $code == 3 ]] || fail 'DB outage exit code'
python3 - "$temp_root/home/.oaos-install/company-state.json" <<'PY' || fail 'blocked state'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    state = json.load(f)
assert state['stages']['c01']['status'] == 'blocked'
PY
printf 'PASS: checksum drift rejected and DB outage blocked\n'

HOME="$temp_root/home" OAOS_STATE_FILE="$temp_root/stage-state.json" OAOS_STATE_EDITION=company OAOS_STAGES='c01 c02' OAOS_NO_LOG_FILE=1 \
  bash -c '. "$1"; stage_mark c01 applied; ! stage_done c01; stage_mark c01 verified; stage_done c01; stage_mark c02 SKIP not_selected; ! stage_done c02' bash "$repo_root/bootstrap/lib/common.sh" || fail 'Company stage semantics'
HOME="$temp_root/home" OAOS_STATE_FILE="$temp_root/personal-state.json" OAOS_STAGES='prep verify' OAOS_NO_LOG_FILE=1 \
  bash -c '. "$1"; stage_mark prep done; stage_done prep' bash "$repo_root/bootstrap/lib/common.sh" || fail 'Personal stage semantics'
printf 'PASS: Company verified/SKIP and Personal done semantics\n'

python3 - "$repo_root/editions/company/migrations/001_company_schema.sql" <<'PY' || fail 'SQL contract'
import pathlib, sys
sql = pathlib.Path(sys.argv[1]).read_text()
for name in ('schema_migrations', 'organizations', 'members', 'external_accounts', 'assistants',
             'secrets', 'policies', 'approvals', 'audit_events', 'documents', 'chunks'):
    assert f'CREATE TABLE company.{name} ' in sql, name
for name in ('members_live_project_user', 'external_accounts_live_subject', 'assistants_live_owner',
             'secrets_active_organization', 'secrets_active_personal'):
    assert f'CREATE UNIQUE INDEX {name}' in sql, name
for name in ('assistants_owner_immutable', 'secrets_owner_immutable', 'documents_owner_immutable'):
    assert f'CREATE TRIGGER {name}' in sql, name
assert sql.count('FOREIGN KEY (org_id,') >= 8
assert sql.count('FORCE ROW LEVEL SECURITY') == 10
PY
printf 'PASS: SQL table, partial index, FK, owner trigger and RLS structure\n'

# A shared origin host can return 200 from another machine, so the Company HTTPS
# evidence must be anchored to this instance's loopback and check the payload.
resolve_helper="--resolve \"\$host:443:127.0.0.1\""
resolve_backup="--resolve \"\$health_host:443:127.0.0.1\""
for file in "$repo_root"/editions/company/stages/c02.sh "$repo_root"/editions/company/verify/c02-verify.sh; do
  if ! grep -Fq -- "$resolve_helper" "$file" && ! grep -Fq -- "$resolve_backup" "$file"; then
    fail "Company HTTPS check in ${file##*/} is not anchored to loopback"
  fi
  grep -Fq 'OAOS_COMPANY_HTTPS_ORIGIN/company/health' "$file" || fail "Company HTTPS check missing in ${file##*/}"
done
if grep -nE '%\{http_code\}' "$repo_root/editions/company/stages/c02.sh" "$repo_root/editions/company/verify/c02-verify.sh" | grep -F 'company/health' >/dev/null; then
  fail 'Company HTTPS check must not rely on a bare status code'
fi
printf 'PASS: Company HTTPS evidence is anchored to this instance\n'

# Read-back rows follow the sections that really passed, not fixed PASS lines.
(
  # shellcheck source=bootstrap/lib/common.sh
  . "$repo_root/bootstrap/lib/common.sh"
  # shellcheck source=editions/company/lib/company.sh
  . "$repo_root/editions/company/lib/company.sh"
  pass=0 fail=0 COMPANY_READBACK=$'owner_isolation\nrolled_back'
  company_readback_check 20 'Owner isolation' owner_isolation >/dev/null
  company_readback_check 20 'Fixture cleanup' tenant_isolation rolled_back >/dev/null
  [[ $pass == 1 && $fail == 1 ]]
) || fail 'read-back check rows'
printf 'PASS: read-back rows reflect emitted section markers\n'

# O7 job credentials: generated once into a 0600 file in the 0700 Company
# config directory; only SCRAM verifiers are printed; unsafe files are refused.
jobs="$repo_root/editions/company/app/db-jobs.py"
mkdir -p "$temp_root/jobs"
chmod 700 "$temp_root/jobs"
python3 "$jobs" ensure "$temp_root/jobs/db-jobs.pgpass" 5432 > "$temp_root/jobs.sql" || fail 'job credential ensure'
[[ $(stat -c %a "$temp_root/jobs/db-jobs.pgpass") == 600 ]] || fail 'job credential file mode'
[[ $(grep -Ec '^127\.0\.0\.1:5432:\*:oaos_company_(backup|maint):[0-9a-f]{64}$' "$temp_root/jobs/db-jobs.pgpass") == 2 ]] || fail 'job credential entries'
[[ $(grep -Ec "^ALTER ROLE oaos_company_(backup|maint) PASSWORD 'SCRAM-SHA-256\\\$4096:[A-Za-z0-9+/=]+\\\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+';\$" "$temp_root/jobs.sql") == 2 ]] || fail 'SCRAM verifier statements'
while IFS=: read -r _ _ _ _ password; do
  ! grep -Fq "$password" "$temp_root/jobs.sql" || fail 'job password printed'
done < "$temp_root/jobs/db-jobs.pgpass"
before=$(sha256sum "$temp_root/jobs/db-jobs.pgpass")
python3 "$jobs" ensure "$temp_root/jobs/db-jobs.pgpass" 5432 > /dev/null || fail 'job credential rerun'
[[ $(sha256sum "$temp_root/jobs/db-jobs.pgpass") == "$before" ]] || fail 'job credentials regenerated on rerun'
chmod 644 "$temp_root/jobs/db-jobs.pgpass"
code=0
python3 "$jobs" ensure "$temp_root/jobs/db-jobs.pgpass" 5432 > "$temp_root/jobs-unsafe.sql" 2>/dev/null || code=$?
[[ $code == 3 && ! -s $temp_root/jobs-unsafe.sql ]] || fail 'world-readable job credential file accepted'
chmod 600 "$temp_root/jobs/db-jobs.pgpass"
chmod 755 "$temp_root/jobs"
code=0
python3 "$jobs" ensure "$temp_root/jobs/db-jobs.pgpass" 5432 > /dev/null 2>&1 || code=$?
[[ $code == 3 ]] || fail 'open job credential directory accepted'
printf 'PASS: job role credentials stay in a 0600 file; only SCRAM verifiers leave it\n'

# O8 evidence JSON: rows, MANUAL items, checkpoint and redacted output with its
# SHA-256; seeded secret values never reach the file.
ev_home="$temp_root/evidence-home"
mkdir -p "$ev_home/.config/oaos-company" "$ev_home/.oaos-install" "$temp_root/ev-rollback/c03-20260101T000000Z.abc123"
chmod 700 "$ev_home/.config/oaos-company"
seed_key=$(python3 -c 'import base64,os; print(base64.b64encode(os.urandom(32)).decode())')
seed_token='seeded-project-token-0123456789'
seed_pass='a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2'
printf 'PGDATABASE=company_e\nOAOS_COMPANY_VAULT_KEY_V1=%s\nOAOS_COMPANY_PROJECT_API_TOKEN=%s\n' "$seed_key" "$seed_token" > "$ev_home/.config/oaos-company/.env"
printf '127.0.0.1:5432:*:oaos_company_backup:%s\n' "$seed_pass" > "$ev_home/.config/oaos-company/db-jobs.pgpass"
printf 'OAOS_COMPANY_E_ID=E-evidence-test\n' > "$ev_home/.config/oaos-company/deployment.conf"
printf '{"edition":"company","stages":{"c03":{"status":"applied"}}}\n' > "$ev_home/.oaos-install/company-state.json"
cat > "$temp_root/ev-out" <<OUT
Migration SHA-256/version 2  PASS
1   Owner isolation          PASS    leaked $seed_key
Real admin login             MANUAL
Debug token=$seed_token password: $seed_pass
Summary: PASS=2 FAIL=0 MANUAL=1 SKIP=0
OUT
ev_path=$(HOME="$ev_home" OAOS_COMPANY_ROLLBACK_DIR="$temp_root/ev-rollback" \
  python3 "$repo_root/editions/company/lib/evidence.py" c03 3 2026-01-01T00:00:00Z 2026-01-01T00:00:05Z "$temp_root/ev-out") || fail 'evidence writer'
[[ $ev_path == "$ev_home/.oaos-install/evidence/company-c03-"*.json ]] || fail 'evidence path'
[[ $(stat -c %a "$ev_path") == 600 && $(stat -c %a "$ev_home/.oaos-install/evidence") == 700 ]] || fail 'evidence modes'
for value in "$seed_key" "$seed_token" "$seed_pass"; do
  ! grep -Fq "$value" "$ev_path" || fail 'seeded secret reached the evidence JSON'
done
python3 - "$ev_path" <<'PY' || fail 'evidence content'
import hashlib, json, sys
doc = json.load(open(sys.argv[1], encoding='utf-8'))
assert doc['phase'] == 'c03' and doc['environment'] == 'E-evidence-test'
assert doc['verify']['exit_code'] == 3 and doc['final_state'] == 'applied'
assert doc['started_at'] == '2026-01-01T00:00:00Z' and doc['finished_at'] == '2026-01-01T00:00:05Z'
assert [c['status'] for c in doc['checks']] == ['PASS', 'PASS', 'MANUAL'], doc['checks']
assert doc['checks'][1]['name'] == 'Owner isolation'
assert [m['name'] for m in doc['manual']] == ['Real admin login']
assert doc['summary'] == {'pass': 2, 'fail': 0, 'manual': 1, 'skip': 0}
assert doc['rollback']['reference'] == 'c03-20260101T000000Z.abc123'
out = doc['stdout']['redacted']
assert hashlib.sha256(out.encode('utf-8')).hexdigest() == doc['stdout']['sha256']
assert '[REDACTED]' in out
PY
printf 'PASS: evidence JSON rows, MANUAL, checkpoint, rollback reference, SHA-256 and secret redaction\n'
