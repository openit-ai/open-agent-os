#!/usr/bin/env bash
# Run with: pg_virtualenv bash -e -o pipefail bootstrap/tests/company-c16-smoke.sh
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
    json.dump({'edition': 'company', 'stages': {
        'c01': {'status': 'verified'}, 'c02': {'status': 'verified'},
        'c03': {'status': 'verified'}, 'c04': {'status': 'verified'},
        'c05': {'status': 'verified'}}}, out)
PY
cat > "$temp_root/bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
case ${1:-} in
  --user) case ${2:-} in
    show-environment) [[ ${OAOS_C16_NO_USER_MANAGER:-0} != 1 ]]; exit $? ;;
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
export OAOS_COMPANY_USER OAOS_COMPANY_E_ID=E-c16-test
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
bash "$repo_root/editions/company/install.sh" --stage c16 > "$temp_root/blocked" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'C07 verified gate'
grep -Fq 'C01, C02, C03, C04, C05 and C07 must be verified' "$temp_root/blocked" || fail 'gate reason'
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
OAOS_C16_NO_USER_MANAGER=1 bash "$repo_root/editions/company/install.sh" --stage c16 > "$temp_root/no-manager" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'systemd user-manager gate'
grep -Fq 'systemd user manager is unavailable' "$temp_root/no-manager" || fail 'systemd gate reason'
code=0
PGPORT=1 bash "$repo_root/editions/company/install.sh" --stage c16 > "$temp_root/no-db" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'database connection gate'
grep -Fq 'E database connection failed' "$temp_root/no-db" || fail 'database gate reason'

psql -X -q -v ON_ERROR_STOP=1 -c 'CREATE ROLE c16_test NOSUPERUSER NOBYPASSRLS' >/dev/null
psql -X -q -v ON_ERROR_STOP=1 -c "GRANT CREATE ON DATABASE \"$PGDATABASE\" TO c16_test" >/dev/null
code=0
env -u PGOPTIONS bash "$repo_root/editions/company/install.sh" --stage c16 > "$temp_root/bypass-role" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'RLS bypass role gate'
grep -Fq 'database role bypasses RLS' "$temp_root/bypass-role" || fail 'RLS bypass gate reason'
export PGOPTIONS='-c role=c16_test'
bash "$repo_root/editions/company/migrate.sh" --through 4 >/dev/null
# C16 configures the single organization created by G10 through its administrator.
env -u PGOPTIONS psql -X -q -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
INSERT INTO company.organizations(slug) VALUES ('install-org');
INSERT INTO company.members(org_id, project_user_id, role, state)
  SELECT id, 'c16-install-admin', 'admin', 'active'
    FROM company.organizations WHERE slug = 'install-org';
SQL

# G13 SKIP still installs the route contract and uses the default model only.
export OAOS_COMPANY_DEFAULT_PROVIDER=opencode-go OAOS_COMPANY_DEFAULT_MODEL=kimi-k2
bash "$repo_root/editions/company/install.sh" --stage c16 > "$temp_root/installed" || fail 'C16 skip install'
[[ $(psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT max(version) FROM company.schema_migrations') == 5 ]] || fail 'migration 5 missing'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'pre-migration backup'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT g13_state FROM company.llm_settings") == skip ]] || fail 'G13 SKIP state'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.llm_routes WHERE task = 'default'") == 1 ]] || fail 'default route'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.llm_models WHERE tier = 'premium'") == 0 ]] || fail 'SKIP added a premium model'
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c16 --read-back > "$temp_root/skip-verified" || fail 'C16 SKIP read-back'
grep -Fq 'Summary: PASS=8 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/skip-verified" || fail 'SKIP read-back summary'

# A usage-billing path without explicit approval must stay blocked and never store a secret.
export OAOS_COMPANY_G13_STATE=selected
export OAOS_COMPANY_G13_PROVIDER=openai OAOS_COMPANY_G13_MODEL=gpt-5.2
export OAOS_COMPANY_G13_API_KEY=sk-c16-test-secret-0123456789
export OAOS_COMPANY_G13_BILLING_MODE=usage OAOS_COMPANY_G13_MONTHLY_CAP_USD=5
export OAOS_COMPANY_G13_BILLING_APPROVED=no
export OAOS_COMPANY_G13_TASKS=complex,research
code=0
bash "$repo_root/editions/company/install.sh" --stage c16 > "$temp_root/unapproved" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'usage billing approval gate'
grep -Fq 'usage billing path requires approval' "$temp_root/unapproved" || fail 'approval gate reason'
! grep -Fq 'sk-c16-test-secret' "$temp_root/unapproved" || fail 'secret leaked to output'
! grep -Fq 'OPENAI_API_KEY=sk-c16-test-secret' "$HOME/.config/oaos-company/.env" || fail 'unapproved key stored'

# Approved usage billing with a monthly cap registers the premium route.
export OAOS_COMPANY_G13_BILLING_APPROVED=yes
bash "$repo_root/editions/company/install.sh" --stage c16 > "$temp_root/selected" || fail 'C16 selected install'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT g13_state FROM company.llm_settings") == selected ]] || fail 'G13 selected state'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.llm_models WHERE tier = 'premium' AND billing_mode = 'usage'") == 1 ]] || fail 'premium model'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.llm_routes WHERE task IN ('complex', 'research')") == 2 ]] || fail 'task routes'
grep -Fq 'OPENAI_API_KEY=sk-c16-test-secret-0123456789' "$HOME/.config/oaos-company/.env" || fail 'approved key not stored'
[[ $(stat -c %a "$HOME/.config/oaos-company/.env") == 600 ]] || fail 'EnvironmentFile mode'
! grep -Fq 'sk-c16-test-secret' "$temp_root/selected" || fail 'secret leaked to selected output'
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c16 --read-back > "$temp_root/verified" || fail 'C16 selected read-back'
grep -Fq 'Summary: PASS=8 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/verified" || fail 'selected read-back summary'
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c01 --read-back > "$temp_root/c01-regression" || fail 'C01 read-back after migration 5'
grep -Fq 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/c01-regression" || fail 'C01 regression summary'
bash "$repo_root/bootstrap/verify/company-verify.sh" --phase c04 --read-back > "$temp_root/c04-regression" || fail 'C04 read-back after migration 5'
grep -Fq 'Summary: PASS=5 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/c04-regression" || fail 'C04 regression summary'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.organizations WHERE slug LIKE 'c16-%'") == 0 ]] || fail 'fixture rollback'
[[ $(python3 - "$HOME/.oaos-install/company-state.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding='utf-8'))['stages']['c16']['status'])
PY
) == verified ]] || fail 'verified checkpoint'
bash "$repo_root/editions/company/install.sh" --stage c16 > "$temp_root/rerun" || fail 'C16 idempotent rerun'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'rerun created unnecessary backup'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.llm_routes WHERE task IN ('complex', 'research')") == 2 ]] || fail 'rerun duplicated routes'
# A rerun without G13 inputs preserves the selected policy instead of downgrading it.
env -u OAOS_COMPANY_G13_STATE -u OAOS_COMPANY_G13_PROVIDER -u OAOS_COMPANY_G13_MODEL \
  -u OAOS_COMPANY_G13_API_KEY -u OAOS_COMPANY_G13_BILLING_MODE \
  -u OAOS_COMPANY_G13_MONTHLY_CAP_USD -u OAOS_COMPANY_G13_BILLING_APPROVED \
  -u OAOS_COMPANY_G13_TASKS bash "$repo_root/editions/company/install.sh" --stage c16 \
  > "$temp_root/preserve" || fail 'C16 preserved-state rerun'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT g13_state FROM company.llm_settings") == selected ]] || fail 'rerun downgraded G13'
printf 'PASS: C16 gates, migration 5, G13 SKIP/selected, approval, cap, backups, idempotence\n'

export OAOS_C16_REPO_ROOT="$repo_root"
python3 - <<'PY'
import os
import sys
sys.path.insert(0, os.path.join(os.environ['OAOS_C16_REPO_ROOT'], 'editions', 'company'))
from llm_router import BillingError, Model, Route, Router, RouteUnavailable

standard = Model('std', 'opencode-go', 'kimi-k2', 'standard', 'flat', 0, 0.0, True)
premium = Model('prem', 'openai', 'gpt-5.2', 'premium', 'usage', 5, 0.0, True)
routes = {'default': Route('default', standard, None),
          'complex': Route('complex', premium, standard)}

calls = []
def ok(model, context):
    calls.append((model.id, context))
    return 'answer:' + model.id

router = Router(routes, {'std': standard, 'prem': premium}, ok)
result = router.run('complex', {'owner': 'owner-a'})
assert result.model_id == 'prem' and result.fallback_used is False
assert result.context == {'owner': 'owner-a'}
assert calls == [('prem', {'owner': 'owner-a'})]

def failing_premium(model, context):
    calls.append((model.id, context))
    if model.id == 'prem':
        raise TimeoutError('provider timeout')
    return 'answer:' + model.id

calls.clear()
router = Router(routes, {'std': standard, 'prem': premium}, failing_premium)
result = router.run('complex', {'owner': 'owner-a'})
assert result.model_id == 'std' and result.fallback_used is True
assert result.reason == 'provider_error' and result.context == {'owner': 'owner-a'}
assert calls == [('prem', {'owner': 'owner-a'}), ('std', {'owner': 'owner-a'})]

# A usage model cannot be selected without an approved cap or after the cap is spent.
unapproved = Model('prem', 'openai', 'gpt-5.2', 'premium', 'usage', 5, 0.0, False)
router = Router(routes, {'std': standard, 'prem': unapproved}, ok)
result = router.run('complex', {'owner': 'owner-a'})
assert result.model_id == 'std' and result.fallback_used is True
assert result.reason == 'billing_not_approved'

spent = Model('prem', 'openai', 'gpt-5.2', 'premium', 'usage', 5, 5.0, True)
router = Router(routes, {'std': standard, 'prem': spent}, ok)
result = router.run('complex', {'owner': 'owner-a'})
assert result.model_id == 'std' and result.reason == 'cap_exceeded'

# With no usable model the router refuses instead of inventing a route.
no_default = {'default': Route('default', unapproved, None)}
router = Router(no_default, {'prem': unapproved}, ok)
try:
    router.run('default', {'owner': 'owner-a'})
except RouteUnavailable:
    pass
else:
    raise AssertionError('unapproved default model was called')

result = Router(routes, {'std': standard, 'prem': premium}, ok).run(
    'missing', {'owner': 'owner-a'})
assert result.model_id == 'std' and result.fallback_used is False
assert result.reason == 'primary_available'
PY
printf 'PASS: router task selection, billing gate, cap and runtime fallback preserve context\n'
