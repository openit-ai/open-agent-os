#!/usr/bin/env bash
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
install="$repo_root/editions/company/install.sh"
verify="$repo_root/bootstrap/verify/company-verify.sh"
runner="$repo_root/editions/company/migrate.sh"
mkdir -p "$temp_root/home/.oaos-install" "$temp_root/bin"

HOME="$temp_root/home" bash "$install" --help | grep -Fq 'c01 through c17' || fail 'install help'
env -u HOME bash "$install" --help > /dev/null || fail 'install help without HOME'
HOME="$temp_root/home" bash "$install" --status > "$temp_root/status"
[[ $(grep -Ec '^c[0-9][0-9] +pending ' "$temp_root/status") == 17 ]] || fail 'empty status'
[[ ! -e $temp_root/home/.oaos-install/company-state.json ]] || fail 'status changed state'
HOME="$temp_root/home" bash "$verify" --help | grep -Fq -- '--read-back' || fail 'verify help'
env -u HOME bash "$verify" --help > /dev/null || fail 'verify help without HOME'
code=0
HOME="$temp_root/home" bash "$install" --stage c02 > /dev/null 2>&1 || code=$?
[[ $code == 3 ]] || fail 'unimplemented stage exit code'
code=0
HOME="$temp_root/home" bash "$install" --stage c01 > /dev/null 2>&1 || code=$?
[[ $code == 3 ]] || fail 'missing Project prerequisite exit code'
HOME="$temp_root/home" bash "$install" --status > "$temp_root/blocked-status"
grep -Eq '^c01 +blocked ' "$temp_root/blocked-status" || fail 'missing Project checkpoint'
code=0
HOME="$temp_root/home" bash "$verify" --phase c01 --read-back > /dev/null 2>&1 || code=$?
[[ $code == 3 ]] || fail 'unapplied verifier exit code'
printf 'PASS: help, read-only status and prerequisite gate\n'

python3 - "$temp_root/home/.oaos-install/state.json" <<'PY'
import json, sys
with open(sys.argv[1], 'w', encoding='utf-8') as f:
    json.dump({'edition': 'project', 'stages': {'stack': {'status': 'done'}, 'verify': {'status': 'done'}}}, f)
PY
cat > "$temp_root/bin/psql" <<'MOCK'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${OAOS_FAKE_DB_FAIL:-0} != 1 ]] || exit 2
if [[ $* == *--single-transaction* ]]; then
  [[ $* == *'001_company_schema.sql'* ]] || exit 2
  [[ $* =~ ([0-9a-f]{64}) ]] || exit 2
  printf '%s\n' "${BASH_REMATCH[1]}" > "$OAOS_FAKE_DB_STATE"
  printf 'apply\n' >> "$OAOS_FAKE_DB_LOG"
elif [[ $* == *'SELECT to_regclass'* ]]; then
  [[ -f $OAOS_FAKE_DB_STATE ]] && printf 't\n' || printf 'f\n'
elif [[ $* == *'SELECT checksum'* ]]; then
  [[ -f $OAOS_FAKE_DB_STATE ]] && cat "$OAOS_FAKE_DB_STATE"
elif [[ $* == *'SELECT COALESCE(MAX(version)'* ]]; then
  if [[ $* == *'COUNT(*)'* ]]; then printf '1:1\n'; else printf '1\n'; fi
elif [[ $* == *'SELECT 1'* ]]; then
  printf '1\n'
else
  exit 2
fi
MOCK
chmod 700 "$temp_root/bin/psql"
export OAOS_FAKE_DB_STATE="$temp_root/db-checksum" OAOS_FAKE_DB_LOG="$temp_root/db-log"
HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" PGDATABASE=company_test bash "$install" --stage c01 > "$temp_root/first"
[[ $(wc -l < "$OAOS_FAKE_DB_LOG") == 1 ]] || fail 'migration was not applied'
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
HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" PGDATABASE=company_test bash "$runner" --check > /dev/null || fail 'checksum check'
printf 'PASS: atomic runner call, checksum skip and applied checkpoint\n'

printf '%064d\n' 0 > "$OAOS_FAKE_DB_STATE"
code=0
HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" PGDATABASE=company_test bash "$runner" --check > /dev/null 2>&1 || code=$?
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
