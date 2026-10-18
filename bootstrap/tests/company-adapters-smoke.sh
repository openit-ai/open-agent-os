#!/usr/bin/env bash
# Run with: pg_virtualenv bash -e -o pipefail bootstrap/tests/company-adapters-smoke.sh
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
[[ -n ${PGPORT:-} ]] || { printf 'Temporary PostgreSQL (pg_virtualenv) is required.\n' >&2; exit 3; }
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
install="$repo_root/editions/company/install.sh"
verify="$repo_root/bootstrap/verify/company-verify.sh"
mkdir -p "$temp_root/home/.oaos-install" "$temp_root/home/.config/oaos-company" \
  "$temp_root/bin" "$temp_root/backup" "$temp_root/key-backup" "$temp_root/rollback"
chmod 700 "$temp_root/home/.config/oaos-company" "$temp_root/backup" "$temp_root/key-backup" "$temp_root/rollback"
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
    show-environment) [[ ${OAOS_C02_NO_USER_MANAGER:-${OAOS_C16_NO_USER_MANAGER:-0}} != 1 ]]; exit $? ;;
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
export OAOS_COMPANY_USER OAOS_COMPANY_E_ID=E-adapters-test
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
python3 - "$HOME/.config/oaos-company/.env" "$PGDATABASE" <<'PY'
import base64, os, sys
with open(sys.argv[1], 'w', encoding='utf-8') as out:
    out.write('PGDATABASE=%s\n' % sys.argv[2])
    out.write('OAOS_COMPANY_VAULT_KEY_V1=%s\n' % base64.b64encode(os.urandom(32)).decode())
PY
chmod 600 "$HOME/.config/oaos-company/.env"

# --- Gates -------------------------------------------------------------------
python3 - "$HOME/.oaos-install/company-state.json" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding='utf-8') as src:
    state = json.load(src)
state['stages']['c05'] = {'status': 'pending'}
with open(path, 'w', encoding='utf-8') as out:
    json.dump(state, out)
PY
code=0
bash "$install" --stage c11 > "$temp_root/prereq" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'C05 verified gate'
grep -Fq 'C01, C02, C03, C04 and C05 must be verified' "$temp_root/prereq" || fail 'prerequisite gate reason'
python3 - "$HOME/.oaos-install/company-state.json" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding='utf-8') as src:
    state = json.load(src)
state['stages']['c05'] = {'status': 'verified'}
with open(path, 'w', encoding='utf-8') as out:
    json.dump(state, out)
PY
code=0
OAOS_C02_NO_USER_MANAGER=1 bash "$install" --stage c12 > "$temp_root/no-manager" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'systemd user-manager gate'
grep -Fq 'systemd user manager is unavailable' "$temp_root/no-manager" || fail 'systemd gate reason'
code=0
PGPORT=1 bash "$install" --stage c13 > "$temp_root/no-db" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'database connection gate'
grep -Fq 'E database connection failed' "$temp_root/no-db" || fail 'database gate reason'

psql -X -q -v ON_ERROR_STOP=1 -c 'CREATE ROLE adapters_test NOSUPERUSER NOBYPASSRLS' >/dev/null
psql -X -q -v ON_ERROR_STOP=1 -c "GRANT CREATE ON DATABASE \"$PGDATABASE\" TO adapters_test" >/dev/null
code=0
env -u PGOPTIONS bash "$install" --stage c11 > "$temp_root/bypass-role" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'RLS bypass role gate'
grep -Fq 'database role bypasses RLS' "$temp_root/bypass-role" || fail 'RLS bypass gate reason'
export PGOPTIONS='-c role=adapters_test'
bash "$repo_root/editions/company/migrate.sh" --through 4 >/dev/null
env -u PGOPTIONS psql -X -q -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
INSERT INTO company.organizations(slug) VALUES ('install-org');
INSERT INTO company.members(org_id, project_user_id, role, state)
  SELECT id, 'adapters-install-admin', 'admin', 'active'
    FROM company.organizations WHERE slug = 'install-org';
SQL
printf 'PASS: gates, migration 1-4 and G10 organization\n'

# --- Slack SKIP is the default and stores no credential ----------------------
bash "$install" --stage c11 > "$temp_root/slack-skip" || fail 'C11 SKIP install'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT max(version) FROM company.schema_migrations') == 7 ]] || fail 'migration 7 missing'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'pre-migration backup'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT state FROM company.integration_settings WHERE provider = 'slack'") == skip ]] || fail 'slack default skip'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.integration_apps") == 0 ]] || fail 'skip created an app'
! grep -Fq 'SLACK_BOT_TOKEN=' "$HOME/.config/oaos-company/.env" || fail 'skip stored a slack secret'

# --- Slack selected ----------------------------------------------------------
export OAOS_COMPANY_SLACK_STATE=selected OAOS_COMPANY_SLACK_WORKSPACE_ID=ws-slack
export OAOS_COMPANY_SLACK_BOT_TOKEN=xoxb-adapters-test-0123456789
export OAOS_COMPANY_SLACK_SIGNING_SECRET=adapters-test-signing-secret
bash "$install" --stage c11 > "$temp_root/slack-selected" || fail 'C11 selected install'
grep -Fq 'SLACK_BOT_TOKEN=xoxb-adapters-test-0123456789' "$HOME/.config/oaos-company/.env" || fail 'slack token not stored'
[[ $(stat -c %a "$HOME/.config/oaos-company/.env") == 600 ]] || fail 'EnvironmentFile mode'
! grep -Fq 'xoxb-adapters-test' "$temp_root/slack-selected" || fail 'slack secret leaked to output'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.integration_apps WHERE provider = 'slack' AND state = 'active'") == 1 ]] || fail 'slack app missing'
bash "$verify" --phase c11 --read-back > "$temp_root/slack-verified" || fail 'C11 read-back'
grep -Fq 'Summary: PASS=8 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/slack-verified" || fail 'C11 summary'

# --- Notion selected ---------------------------------------------------------
export OAOS_COMPANY_NOTION_STATE=selected OAOS_COMPANY_NOTION_WORKSPACE_ID=ws-notion
export OAOS_COMPANY_NOTION_TOKEN=secret-adapters-notion-token
bash "$install" --stage c12 > "$temp_root/notion-selected" || fail 'C12 selected install'
! grep -Fq 'secret-adapters-notion-token' "$temp_root/notion-selected" || fail 'notion secret leaked to output'
bash "$verify" --phase c12 --read-back > "$temp_root/notion-verified" || fail 'C12 read-back'
grep -Fq 'Summary: PASS=8 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/notion-verified" || fail 'C12 summary'

# --- Google Workspace selected; Microsoft 365 conflicts and is then SKIP -----
export OAOS_COMPANY_GWS_STATE=selected OAOS_COMPANY_GWS_WORKSPACE_ID=example.test
export OAOS_COMPANY_GWS_REDIRECT_URI=https://e.example.test/company/oauth/callback
export OAOS_COMPANY_GWS_CLIENT_ID=adapters-gws-client
export OAOS_COMPANY_GWS_CLIENT_SECRET=adapters-gws-secret
bash "$install" --stage c13 > "$temp_root/gws-selected" || fail 'C13 selected install'
! grep -Fq 'adapters-gws-secret' "$temp_root/gws-selected" || fail 'gws secret leaked to output'
bash "$verify" --phase c13 --read-back > "$temp_root/gws-verified" || fail 'C13 read-back'
grep -Fq 'Summary: PASS=8 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/gws-verified" || fail 'C13 summary'

code=0
OAOS_COMPANY_M365_STATE=selected OAOS_COMPANY_M365_WORKSPACE_ID=tenant-1 \
  OAOS_COMPANY_M365_TENANT_ID=tenant-1 OAOS_COMPANY_M365_REDIRECT_URI=https://e.example.test/company/oauth/callback \
  OAOS_COMPANY_M365_CLIENT_ID=adapters-m365-client OAOS_COMPANY_M365_CLIENT_SECRET=adapters-m365-secret \
  bash "$install" --stage c14 > "$temp_root/m365-conflict" 2>&1 || code=$?
[[ $code == 3 ]] || fail 'suite conflict gate'
grep -Fq 'mutually exclusive' "$temp_root/m365-conflict" || fail 'suite conflict reason'
OAOS_COMPANY_M365_STATE=skip bash "$install" --stage c14 > "$temp_root/m365-skip" || fail 'C14 SKIP install'

# --- C15 gate close ----------------------------------------------------------
bash "$install" --stage c15 > "$temp_root/gate" || fail 'C15 gate install'
bash "$verify" --phase c15 --read-back > "$temp_root/gate-verified" || fail 'C15 read-back'
grep -Fq 'Summary: PASS=5 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/gate-verified" || fail 'C15 summary'
bash "$verify" --phase c14 --read-back > "$temp_root/m365-skip-verified" || fail 'C14 SKIP read-back'
grep -Fq 'Summary: PASS=8 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/m365-skip-verified" || fail 'C14 SKIP summary'
printf 'PASS: adapters selected/SKIP, read-back, suite exclusivity and C15 gate\n'

# --- Idempotence and regression ---------------------------------------------
bash "$install" --stage c11 > "$temp_root/slack-rerun" || fail 'C11 idempotent rerun'
[[ $(find "$OAOS_COMPANY_ROLLBACK_DIR" -name company.dump | wc -l) == 1 ]] || fail 'rerun created unnecessary backup'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.integration_apps WHERE provider = 'slack' AND state = 'active'") == 1 ]] || fail 'rerun duplicated apps'
bash "$verify" --phase c01 --read-back > "$temp_root/c01-regression" || fail 'C01 read-back after migration 7'
grep -Fq 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/c01-regression" || fail 'C01 regression summary'
bash "$verify" --phase c04 --read-back > "$temp_root/c04-regression" || fail 'C04 read-back after migration 7'
grep -Fq 'Summary: PASS=5 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/c04-regression" || fail 'C04 regression summary'
bash "$verify" --phase c05 --read-back > "$temp_root/c05-regression" || fail 'C05 read-back after migration 7'
grep -Fq 'Summary: PASS=7 FAIL=0 MANUAL=0 SKIP=0' "$temp_root/c05-regression" || fail 'C05 regression summary'
[[ $(env -u PGOPTIONS psql -X -q -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM company.organizations WHERE slug LIKE 'adapt-%'") == 0 ]] || fail 'fixture rollback'
printf 'PASS: idempotence, backups and C01/C04/C05 regression\n'

# --- Runtime adapters --------------------------------------------------------
export OAOS_ADAPTERS_REPO_ROOT="$repo_root"
python3 - <<'PY'
import os
import sys
sys.path.insert(0, os.path.join(os.environ['OAOS_ADAPTERS_REPO_ROOT'], 'editions', 'company'))
from integration_adapters import (
    AdapterDenied, Delegation, IntegrationRegistry, NotionAdapter,
    SlackAdapter, SlackEvent, SuiteAdapter, scopes_allowed,
)

# A skipped provider is never dispatched.
registry = IntegrationRegistry({'slack': 'skip'}, apps=['slack'])
try:
    registry.dispatch('slack', lambda: 'called')
except AdapterDenied as exc:
    assert exc.reason == 'not_selected'
else:
    raise AssertionError('skipped provider was dispatched')

# A selected provider without an organization app is refused.
registry = IntegrationRegistry({'slack': 'selected'})
try:
    registry.dispatch('slack', lambda: 'called')
except AdapterDenied as exc:
    assert exc.reason == 'app_missing'
else:
    raise AssertionError('selected provider without app was dispatched')

# Slack signature, timestamp and replay verification.
registry = IntegrationRegistry({'slack': 'selected'}, apps=['slack'])
routed_members = {('T1', 'U1'): {'member_id': 'm1', 'active': True}}
slack = SlackAdapter(registry, lambda team, user: routed_members.get((team, user)),
                     lambda member: 'a1' if member == 'm1' else None)
event = SlackEvent('T1', 'Ev1', 'U1', 'C1', 'hello')
body = b'{"event_id":"Ev1"}'
stamp = '1700000000'
signature = SlackAdapter.signature('topsecret', stamp, body)
seen = set()
slack.verify_event('topsecret', stamp, signature, body, 1700000000, seen, event)
try:
    slack.verify_event('topsecret', stamp, signature, body, 1700000000, seen, event)
except AdapterDenied as exc:
    assert exc.reason == 'duplicate_event'
else:
    raise AssertionError('replayed event was accepted')
try:
    slack.verify_event('topsecret', stamp, 'v0=deadbeef', body, 1700000000, set(), event)
except AdapterDenied as exc:
    assert exc.reason == 'signature_mismatch'
else:
    raise AssertionError('bad signature was accepted')
try:
    slack.verify_event('topsecret', '1', signature, body, 1700000000, set(), event)
except AdapterDenied as exc:
    assert exc.reason == 'stale_timestamp'
else:
    raise AssertionError('stale event was accepted')
routed = slack.route(event)
assert routed.member_id == 'm1' and routed.assistant_id == 'a1'
try:
    slack.route(SlackEvent('T1', 'Ev2', 'U-unknown', 'C1', 'hi'))
except AdapterDenied as exc:
    assert exc.reason == 'unregistered'
else:
    raise AssertionError('unknown sender was routed')

# Notion only exposes explicitly shared pages with a confirmed ACL version.
notion = NotionAdapter(IntegrationRegistry({'notion': 'selected'}, apps=['notion']))
candidate = notion.candidate({'page-1'}, 'page-1', 'acl-v1', 'body')
assert notion.readable(candidate, 'acl-v1')
assert not notion.readable(candidate, 'acl-v2')
assert not notion.readable(candidate, None)
try:
    notion.candidate({'page-1'}, 'page-2', 'acl-v1', 'body')
except AdapterDenied as exc:
    assert exc.reason == 'not_shared'
else:
    raise AssertionError('unshared page was indexed')
try:
    notion.candidate({'page-1'}, 'page-1', '', 'body')
except AdapterDenied as exc:
    assert exc.reason == 'acl_unconfirmed'
else:
    raise AssertionError('page without ACL version was indexed')

# Delegated suite access is read-only and owner-bound.
assert scopes_allowed('google_workspace', ['https://www.googleapis.com/auth/gmail.readonly'])
assert not scopes_allowed('google_workspace', ['https://www.googleapis.com/auth/gmail.modify'])
assert not scopes_allowed('microsoft_365', ['Mail.ReadWrite'])
assert not scopes_allowed('microsoft_365', ['Mail.Read', 'Mail.ReadWrite'])
suite = SuiteAdapter(IntegrationRegistry({'google_workspace': 'selected'},
                                         apps=['google_workspace']), 'google_workspace')
delegation = Delegation('owner-a', 'google_workspace', 'subject-1',
                        ('https://www.googleapis.com/auth/gmail.readonly',))
assert suite.authorize('owner-a', delegation, delegation.scopes) is delegation
try:
    suite.authorize('owner-b', delegation, delegation.scopes)
except AdapterDenied as exc:
    assert exc.reason == 'owner_mismatch'
else:
    raise AssertionError('cross-owner delegation was allowed')
try:
    suite.authorize('owner-a', delegation, ['https://www.googleapis.com/auth/gmail.modify'])
except AdapterDenied as exc:
    assert exc.reason == 'invalid_scope'
else:
    raise AssertionError('write scope was authorized')
try:
    suite.authorize('owner-a', Delegation('owner-a', 'google_workspace', 'subject-1',
                                          delegation.scopes, active=False), delegation.scopes)
except AdapterDenied as exc:
    assert exc.reason == 'delegation_unavailable'
else:
    raise AssertionError('revoked delegation was allowed')
PY
printf 'PASS: adapters skip/selection, slack signature/replay, notion ACL, owner-bound read-only delegation\n'
printf 'PASS: company adapters end-to-end\n'
