#!/usr/bin/env bash
# Project portal (ADR 0002): application, mail code, Mattermost approval, invites, allowlist
# sync, audit, retention, page rendering, backup and the portal install stage, against local
# mock Mattermost/Outline/SMTP servers. No real host service is touched.
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
pids=()
cleanup() { local pid; for pid in ${pids[@]+"${pids[@]}"}; do kill "$pid" 2>/dev/null || true; done; rm -rf -- "$temp_root"; }
trap cleanup EXIT
for name in ${!OAOS_@} ${!MATTERMOST_@} ${!OUTLINE_@}; do unset "$name"; done
unset HERMES_HOME
server="$repo_root/editions/project/portal/server.py"
render="$repo_root/editions/project/portal/render-site.py"
fail() {
  printf 'FAIL: %s\n' "$1" >&2
  for file in "$temp_root"/*.log "$temp_root"/*.out "$temp_root"/*.err; do [[ -f $file ]] && { printf '\n--- %s ---\n' "$file" >&2; tail -n 40 "$file" >&2; }; done
  exit 1
}
wait_file() { local _; for _ in $(seq 1 50); do [[ -s $1 ]] && return 0; sleep 0.1; done; return 1; }
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }

state="$temp_root/mocks"
mkdir -p "$state"
mm_token='mmbottoken0123456789abcdef'
ol_token='ol_api_agenttoken0123456789abcdefghijklmnopqr'
mail_password='botapppassword0123'
callback_secret=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
MOCK_MM_TOKEN=$mm_token MOCK_OUTLINE_TOKEN=$ol_token MOCK_SMTP_USER=bot@example.com MOCK_SMTP_PASSWORD=$mail_password \
  python3 "$repo_root/bootstrap/tests/fixtures/portal_mocks.py" "$state" 2>"$temp_root/mocks.err" &
pids+=($!)
wait_file "$state/http.port" && wait_file "$state/smtp.port" || fail 'mock servers did not start'
http_port=$(cat "$state/http.port")
smtp_port=$(cat "$state/smtp.port")

admin=$(printf 'a%.0s' {1..26}); member=$(printf 'm%.0s' {1..26}); lead=$(printf 'c%.0s' {1..26})
selfadmin=$(printf 's%.0s' {1..26}); existing=$(printf 'e%.0s' {1..26}); team=$(printf 't%.0s' {1..26})
home="$temp_root/home"
mkdir -p "$home/.hermes"
chmod 700 "$home/.hermes"
cat > "$home/.hermes/.env" <<EOF
MATTERMOST_TOKEN=$mm_token
MATTERMOST_ALLOWED_USERS=$existing
OUTLINE_API_TOKEN=$ol_token
OAOS_MAIL_ADDRESS=bot@example.com
OAOS_MAIL_PASSWORD=$mail_password
OAOS_MAIL_SMTP_HOST=127.0.0.1
OAOS_PORTAL_CALLBACK_SECRET=$callback_secret
OAOS_PORTAL_ADMINS=$admin
OAOS_PORTAL_TEAM_ID=$team
EOF
chmod 600 "$home/.hermes/.env"
port=$(free_port)
portal_env=(HOME="$home" HERMES_HOME="$home/.hermes" OAOS_TEST_MODE=1 OAOS_PORTAL_PORT="$port"
  OAOS_PORTAL_PUBLIC_URL=https://portal.example.com OAOS_PORTAL_CHAT_URL=https://chat.example.com
  OAOS_PORTAL_NOTE_URL=https://note.example.com OAOS_MATTERMOST_API="http://127.0.0.1:$http_port"
  OAOS_OUTLINE_API="http://127.0.0.1:$http_port" OAOS_PORTAL_SMTP_PORT="$smtp_port" OAOS_PORTAL_SMTP_PLAIN=1
  OAOS_PORTAL_SYNC_SECONDS=3600 OAOS_PORTAL_IP_LIMIT=6
  OAOS_PORTAL_RESTART_CMD="touch $temp_root/gateway-restarted")
env "${portal_env[@]}" python3 -I "$server" 2>"$temp_root/server.err" &
pids+=($!)
base="http://127.0.0.1:$port"
for _ in $(seq 1 50); do curl -fsS "$base/api/portal/health" >/dev/null 2>&1 && break; sleep 0.1; done
health=$(curl -fsS "$base/api/portal/health") || fail 'portal health endpoint'
[[ $health == *'"status": "ok"'* && $health == *'"apply": "on"'* ]] || fail "unexpected health: $health"
[[ $(stat -c '%a' "$home/oaos/portal") == 700 && $(stat -c '%a' "$home/oaos/portal/portal.db") == 600 ]] || fail 'portal directory/database modes'
printf 'PASS: service starts on loopback; portal directory 700 and portal.db 600\n'

sql() { python3 -c 'import sqlite3, sys; db = sqlite3.connect(sys.argv[1]); rows = db.execute(sys.argv[2]).fetchall(); db.commit(); print("\n".join("|".join("" if v is None else str(v) for v in r) for r in rows))' "$home/oaos/portal/portal.db" "$1"; }
mail_count() { find "$state/mail" -name '*.eml' 2>/dev/null | wc -l; }
# Prints the newest captured mail as decoded text (subject, recipient, body).
last_mail() {
  find "$state/mail" -name '*.eml' | sort | tail -n 1 | xargs cat | python3 -c '
import email, email.header, sys
m = email.message_from_string(sys.stdin.read())
print(str(email.header.make_header(email.header.decode_header(m["Subject"]))))
print(m["To"])
print(m.get_payload(decode=True).decode())'
}
last_code() { last_mail | grep -oE '\b[0-9]{6}\b' | head -n1; }
jar="$temp_root/cookies"
open_form() { rm -f "$jar"; curl -fsS -c "$jar" "$base/apply" > "$temp_root/form.out"; csrf=$(sed -n 's/.*name="csrf" value="\([^"]*\)".*/\1/p' "$temp_root/form.out" | head -n1); [[ -n $csrf ]] || fail 'apply form has no CSRF token'; }
post() { curl -sS -o "$temp_root/post.out" -w '%{http_code}' -b "$jar" -H 'X-Real-IP: 203.0.113.7' "$base$1" "${@:2}"; }
apply() {
  post /api/portal/apply --data-urlencode "csrf=$csrf" --data-urlencode "name=$1" --data-urlencode "email=$2" \
    --data-urlencode 'org=OpenIT' --data-urlencode 'purpose=team chat and wiki' --data-urlencode 'consent=yes' "${@:3}"
}
request_id() { sed -n 's/.*name="request" value="\([^"]*\)".*/\1/p' "$temp_root/post.out" | head -n1; }

# --- application and mail code --------------------------------------------------------------
open_form
curl -fsS -D "$temp_root/headers.out" -o /dev/null "$base/apply"; grep -Fq 'Secure; HttpOnly; SameSite=Strict' "$temp_root/headers.out" || fail 'CSRF cookie attributes'
code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$base/api/portal/apply" --data 'name=x&email=x@example.com&consent=yes&purpose=x')
[[ $code == 403 ]] || fail "POST without the CSRF cookie answered $code"
[[ $(apply Bot spam@example.com --data-urlencode 'website=http://spam') == 200 ]] || fail 'honeypot response'
[[ $(sql "SELECT COUNT(*) FROM requests") == 0 && $(mail_count) == 0 ]] || fail 'honeypot submission created a request or a mail'
[[ $(post /api/portal/apply --data-urlencode "csrf=$csrf" --data-urlencode 'name=Kim' --data-urlencode 'email=kim@example.com' --data-urlencode 'purpose=x') == 400 ]] ||
  fail 'missing consent accepted'
[[ $(apply 'Applicant' applicant@example.com) == 200 ]] || fail 'valid application'
rid=$(request_id)
[[ -n $rid && $(sql "SELECT state FROM requests WHERE id = '$rid'") == unverified ]] || fail 'application not stored as unverified'
[[ $(mail_count) == 1 ]] || fail 'verification mail not sent'
code=$(last_code)
[[ $(post /api/portal/verify --data-urlencode "csrf=$csrf" --data-urlencode "request=$rid" --data-urlencode 'code=000000') == 400 ]] || fail 'wrong code accepted'
[[ $(post /api/portal/verify --data-urlencode "csrf=$csrf" --data-urlencode "request=$rid" --data-urlencode "code=$code") == 200 ]] || fail 'correct code rejected'
[[ $(sql "SELECT state FROM requests WHERE id = '$rid'") == pending ]] || fail 'request not pending after the code'
[[ -z $(sql "SELECT code_hash FROM requests WHERE id = '$rid'") ]] || fail 'code hash kept after verification'
python3 - "$state/http.log" "$admin" <<'PY' || fail 'administrator DM with signed buttons'
import json, sys
posts = [json.loads(l) for l in open(sys.argv[1]) if '"/api/v4/posts"' in l]
direct = [json.loads(l) for l in open(sys.argv[1]) if '"/api/v4/channels/direct"' in l]
assert len(posts) == 1 and direct[-1]["body"][1] == sys.argv[2]
actions = posts[0]["body"]["props"]["attachments"][0]["actions"]
assert [a["id"] for a in actions] == ["approve", "reject"]
assert all(a["integration"]["url"] == "https://portal.example.com/api/portal/decide" and len(a["integration"]["context"]["sig"]) == 64 for a in actions)
PY
[[ $(apply 'Applicant' applicant@example.com) == 200 ]] && grep -Fq 'already in progress' "$temp_root/post.out" || fail 'duplicate application for a pending mail'
printf 'PASS: CSRF, honeypot, consent, 6-digit mail code, pending request and signed admin DM\n'

# --- Mattermost decision callback ----------------------------------------------------------
context() { python3 -c 'import hashlib, hmac, json, sys; s, r, a = sys.argv[1:4]; print(json.dumps({"request_id": r, "action": a, "sig": hmac.new(s.encode(), ("decide:%s:%s" % (r, a)).encode(), hashlib.sha256).hexdigest()}))' "$callback_secret" "$1" "$2"; }
decide() { curl -sS -o "$temp_root/decide.out" -w '%{http_code}' -H 'Content-Type: application/json' -X POST "$base/api/portal/decide" \
  --data "{\"user_id\":\"$1\",\"team_id\":\"$team\",\"context\":$2}"; }
forged=$(context "$rid" approve | sed 's/"sig": "[0-9a-f]/"sig": "0/')
[[ $(decide "$admin" "$forged") == 403 ]] || fail 'forged signature accepted'
[[ $(decide "$admin" "$(context "$rid" reject | sed 's/"action": "reject"/"action": "approve"/')") == 403 ]] || fail 'signature of another action accepted'
[[ $(decide "$member" "$(context "$rid" approve)") == 200 ]] && grep -Fq 'Only a Project administrator' "$temp_root/decide.out" || fail 'non-admin decision not refused'
[[ $(decide "$selfadmin" "$(context "$rid" approve)") == 200 ]] && grep -Fq 'own request' "$temp_root/decide.out" || fail 'applicant decided their own request'
[[ $(sql "SELECT state FROM requests WHERE id = '$rid'") == pending ]] || fail 'a refused decision changed the request'
mails_before=$(mail_count)
[[ $(decide "$lead" "$(context "$rid" approve)") == 200 ]] || fail 'team admin approval'
grep -Fq '"update"' "$temp_root/decide.out" && grep -Fq 'Approved' "$temp_root/decide.out" || fail 'approval did not update the message'
[[ $(sql "SELECT state || ' ' || invite_mattermost || ' ' || invite_outline FROM requests WHERE id = '$rid'") == 'approved email invited' ]] || fail 'invites not recorded'
[[ $(sql "SELECT decider_id FROM decisions WHERE request_id = '$rid'") == "$lead" ]] || fail 'decision row'
grep -F '"/api/v4/teams/'"$team"'/invite/email"' "$state/http.log" | grep -Fq 'applicant@example.com' || fail 'Mattermost team invite not sent'
grep -F '/api/users.invite' "$state/http.log" | grep -Fq '"role": "member"' || fail 'Outline invite not sent as member'
(( $(mail_count) == mails_before + 1 )) && last_mail | grep -Fq 'approved' || fail 'approval result mail'
[[ $(decide "$admin" "$(context "$rid" reject)") == 200 ]] && grep -Fq 'Already approved' "$temp_root/decide.out" || fail 'second decision not refused'
printf 'PASS: signature, role re-read (system/team admin), self-decision refusal, invites and result mail\n'

# Invite fallbacks and rejection.
touch "$state/mm-invite-off" "$state/outline-invite-off"
open_form
[[ $(apply 'Second' second@example.com) == 200 ]] || fail 'second application'
rid2=$(request_id)
post /api/portal/verify --data-urlencode "csrf=$csrf" --data-urlencode "request=$rid2" --data-urlencode "code=$(last_code)" >/dev/null
[[ $(decide "$admin" "$(context "$rid2" approve)") == 200 ]] || fail 'approval with invites off'
grep -Fq 'manually' "$temp_root/decide.out" || fail 'administrator to-do for manual Outline invite'
[[ $(sql "SELECT invite_mattermost FROM requests WHERE id = '$rid2'") == link:https://chat.example.com/signup_user_complete/?id=invite0123456789abcdef ]] || fail 'team invite link fallback'
last_mail | grep -Fq 'signup_user_complete/?id=invite0123456789abcdef' || fail 'invite link missing from the result mail'
open_form
[[ $(apply 'Third' third@example.com) == 200 ]] || fail 'third application'
rid3=$(request_id)
post /api/portal/verify --data-urlencode "csrf=$csrf" --data-urlencode "request=$rid3" --data-urlencode "code=$(last_code)" >/dev/null
[[ $(decide "$admin" "$(context "$rid3" reject)") == 200 ]] && grep -Fq 'Rejected' "$temp_root/decide.out" || fail 'rejection'
last_mail | grep -Fq 'not approved' || fail 'rejection mail'
printf 'PASS: invite-link and manual Outline fallbacks; rejection mail\n'

# --- allowlist sync, retention, audit -------------------------------------------------------
signed=$(printf 'n%.0s' {1..26})
printf '{"applicant@example.com": "%s"}\n' "$signed" > "$state/signups.json"
[[ $(env "${portal_env[@]}" python3 -I "$server" --sync-once 2>"$temp_root/sync.err") == 1 ]] || fail 'sync did not match the signed-up applicant'
grep -Fqx "MATTERMOST_ALLOWED_USERS=$existing,$signed" "$home/.hermes/.env" || fail 'allowlist not extended in place'
grep -Fqx "MATTERMOST_TOKEN=$mm_token" "$home/.hermes/.env" || fail 'other .env entries changed'
[[ $(stat -c '%a' "$home/.hermes/.env") == 600 ]] || fail 'Hermes .env lost mode 600'
[[ -f $temp_root/gateway-restarted ]] || fail 'gateway not restarted after the allowlist change'
rm -f "$temp_root/gateway-restarted"
[[ $(env "${portal_env[@]}" python3 -I "$server" --sync-once 2>>"$temp_root/sync.err") == 0 && ! -e $temp_root/gateway-restarted ]] || fail 'sync repeated work'
sql "UPDATE requests SET decided_at = decided_at - 40 * 86400 WHERE id = '$rid3'" >/dev/null
env "${portal_env[@]}" python3 -I "$server" --sync-once >/dev/null 2>>"$temp_root/sync.err"
[[ $(sql "SELECT COALESCE(name, '') || COALESCE(email, '') || '|' || (purged_at IS NOT NULL) FROM requests WHERE id = '$rid3'") == '|1' ]] || fail 'rejected request kept personal data past retention'
[[ -n $(sql "SELECT email FROM requests WHERE id = '$rid'") ]] || fail 'approved request lost its data'
if sql "UPDATE audit SET result = 'x'" >/dev/null 2>&1 || sql "DELETE FROM audit" >/dev/null 2>&1; then fail 'audit rows could be changed'; fi
for action in apply verify notify-admins decide invite result-mail allowlist gateway-restart purge; do
  [[ -n $(sql "SELECT 1 FROM audit WHERE action = '$action' LIMIT 1") ]] || fail "audit lacks $action"
done
[[ -z $(sql "SELECT detail FROM audit WHERE detail LIKE '%@%'") ]] || fail 'audit holds a mail address'
printf 'PASS: allowlist sync with one gateway restart, retention purge, append-only audit without mail addresses\n'

# --- limits and switch ------------------------------------------------------------------------
open_form
limited=0
for n in 1 2 3 4 5 6 7; do [[ $(apply "R$n" "r$n@example.com") == 429 ]] && { limited=1; break; }; done
((limited)) || fail 'per-IP rate limit not applied'
printf 'OAOS_PORTAL_APPLY=off\n' >> "$home/.hermes/.env"
curl -fsS "$base/apply" | grep -Fq 'Applications are closed' || fail 'apply=off still shows the form'
[[ $(curl -fsS "$base/api/portal/health") == *'"apply": "off"'* ]] || fail 'health does not report apply=off'
sed -i '/^OAOS_PORTAL_APPLY=off$/d' "$home/.hermes/.env"
for secret in "$mm_token" "$ol_token" "$mail_password" "$callback_secret"; do
  grep -Fq "$secret" "$temp_root/server.err" "$temp_root/sync.err" && fail 'a secret reached the service log'
done
printf 'PASS: per-IP rate limit, OAOS_PORTAL_APPLY=off, no secret in logs\n'

# --- page rendering, candidates export, backup snapshot ------------------------------------
printf '# 우리 팀\n\n<script>alert(1)</script> **환영** [가이드](https://example.com/guide)\n\n- 하나\n' > "$temp_root/site.md"
python3 -I "$render" "$temp_root/site.md" chat.example.com note.example.com > "$temp_root/index.html"
[[ $(head -n1 "$temp_root/index.html") == '<!-- OAOS Project managed portal -->' ]] || fail 'managed marker not on the first line'
grep -Fq '&lt;script&gt;' "$temp_root/index.html" && ! grep -Fq '<script>' "$temp_root/index.html" || fail 'operator HTML not escaped'
grep -Fq '<a href="https://example.com/guide" rel="noopener">가이드</a>' "$temp_root/index.html" || fail 'operator link'
grep -Fq 'href="/apply"' "$temp_root/index.html" && grep -Fq 'https://chat.example.com' "$temp_root/index.html" || fail 'guide links'
python3 -I "$render" "$temp_root/absent.md" chat.example.com note.example.com | grep -Fq '소개' || fail 'default introduction'
python3 -I "$repo_root/editions/project/portal/export-candidates.py" "$home/oaos/portal/portal.db" > "$temp_root/candidates.json"
python3 - "$temp_root/candidates.json" "$signed" <<'PY' || fail 'member candidates export'
import json, sys
doc = json.load(open(sys.argv[1]))
emails = sorted(c["email"] for c in doc["candidates"])
assert emails == ["applicant@example.com", "second@example.com"], emails
first = [c for c in doc["candidates"] if c["email"] == "applicant@example.com"][0]
assert first["project_user_id"] == sys.argv[2] and first["state"] == "invited" and first["role"] == "member"
PY
HOME="$home" HERMES_HOME="$home/.hermes" OAOS_BACKUP_DIR="$temp_root/backups" bash "$repo_root/editions/project/backup.sh" > "$temp_root/backup.out" 2>"$temp_root/backup.err" || fail 'backup with the portal'
archive=$(cat "$temp_root/backup.out")
mkdir "$temp_root/unpacked" && tar -xf "$archive" -C "$temp_root/unpacked"
grep -Fqx 'portal portal.db' "$temp_root/unpacked/manifest.txt" || fail 'manifest lacks the portal snapshot'
[[ $(python3 -c 'import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT COUNT(*) FROM requests").fetchone()[0])' "$temp_root/unpacked/portal.db") -ge 3 ]] || fail 'portal snapshot incomplete'
tar -tzf "$temp_root/unpacked/home.tar.gz" > "$temp_root/home-list.out"
grep -Fq 'oaos/portal/bin' "$temp_root/home-list.out" || grep -Fqx 'oaos/portal/' "$temp_root/home-list.out" || fail 'portal directory not in the home archive'
grep -q 'oaos/portal/portal.db' "$temp_root/home-list.out" && fail 'live portal.db copied into the home archive'
HOME="$home" HERMES_HOME="$home/.hermes" bash "$repo_root/editions/project/restore.sh" --dry-run "$archive" > "$temp_root/restore.out" 2>&1 || fail 'restore dry-run'
grep -Fq 'Would restore the portal database' "$temp_root/restore.out" || fail 'restore plan lacks the portal database'
printf 'PASS: escaped operator page with the managed marker, member candidates, portal backup snapshot and restore plan\n'

# --- install stage ----------------------------------------------------------------------------
stage_home="$temp_root/stage-home"
stage_bin="$temp_root/stage-bin"
sys_root="$temp_root/sys"
mkdir -p "$stage_home/oaos/stack" "$stage_home/.hermes" "$stage_home/.oaos-install" "$stage_bin" "$sys_root/etc/nginx/sites-available" "$sys_root/var/www/oaos-portal"
chmod 700 "$stage_home/.hermes"
printf 'CHAT_DOMAIN=chat.example.com\nNOTE_DOMAIN=note.example.com\nPORTAL_DOMAIN=portal.example.com\n' > "$stage_home/oaos/stack/.env"
printf 'MATTERMOST_TOKEN=%s\nOAOS_MAIL_ADDRESS=bot@example.com\n' "$mm_token" > "$stage_home/.hermes/.env"
chmod 600 "$stage_home/oaos/stack/.env" "$stage_home/.hermes/.env"
cat > "$stage_home/.oaos-install/state.json" <<'JSON'
{"edition": "project", "updated_at": "2026-01-01T00:00:00Z", "stages": {"team": {"status": "done"}},
 "editions": {"project": {"stages": {"team": {"status": "done"}}}}}
JSON
printf '# OAOS Project managed vhost\n# old template without the portal API\n' > "$sys_root/etc/nginx/sites-available/oaos-portal.conf"
printf '<!-- OAOS Project managed portal -->\nold page\n' > "$sys_root/var/www/oaos-portal/index.html"
cat > "$stage_bin/sudo" <<'MOCK'
#!/usr/bin/env bash
[[ $1 == -n ]] && shift
[[ $1 == true ]] && exit 0
exec "$@"
MOCK
cat > "$stage_bin/runuser" <<'MOCK'
#!/usr/bin/env bash
while (($#)) && [[ $1 != -- ]]; do shift; done
shift
exec "$@"
MOCK
cat > "$stage_bin/mmctl" <<MOCK
#!/usr/bin/env bash
case "\$*" in
  *'user list'*) printf '[{"id":"$admin","is_bot":false},{"id":"$(printf 'b%.0s' {1..26})","is_bot":true}]' ;;
  *ShowEmailAddress*) printf 'PrivacySettings.ShowEmailAddress: true\n' ;;
  *EnableEmailInvitations*) printf 'ServiceSettings.EnableEmailInvitations: false\n' ;;
esac
MOCK
cat > "$stage_bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$OAOS_FAKE_SYSTEMCTL_LOG"
MOCK
printf '#!/usr/bin/env bash\nexit 0\n' > "$stage_bin/hermes"
chmod 700 "$stage_bin"/*
stage_port=$(free_port)
run_stage() {
  env HOME="$stage_home" PATH="$stage_bin:$PATH" OAOS_TEST_MODE=1 OAOS_TEST_SYSTEM_ROOT="$sys_root" OAOS_ALLOW_FORK=1 \
    OAOS_TEST_MMCTL="$stage_bin/mmctl" OAOS_TEST_PORTAL_PORT="$stage_port" OAOS_MATTERMOST_API="http://127.0.0.1:$http_port" \
    OAOS_FAKE_SYSTEMCTL_LOG="$temp_root/systemctl.log" bash "$repo_root/editions/project/install.sh" --stage portal --recheck --skip-verify
}
code=0
run_stage > "$temp_root/stage1.out" 2>&1 || code=$?
[[ $code == 3 ]] && grep -Fq -- '--stage ingress --recheck' "$temp_root/stage1.out" || fail 'portal stage did not ask for the ingress rerun on an old vhost'
printf '# OAOS Project managed vhost\nlocation /api/portal/ {}\n' > "$sys_root/etc/nginx/sites-available/oaos-portal.conf"
# The test starts the service itself (systemctl is mocked) on the stage port.
env "${portal_env[@]}" HOME="$stage_home" HERMES_HOME="$stage_home/.hermes" OAOS_PORTAL_PORT="$stage_port" python3 -I "$server" 2>"$temp_root/stage-server.err" &
pids+=($!)
code=0
run_stage > "$temp_root/stage2.out" 2>&1 || code=$?
[[ $code == 0 ]] || fail "portal stage exit $code"
env_file="$stage_home/.hermes/.env"
grep -Eq '^OAOS_PORTAL_CALLBACK_SECRET=[0-9a-f]{64}$' "$env_file" || fail 'callback secret not created'
grep -Fqx "OAOS_PORTAL_ADMINS=$admin" "$env_file" || fail 'administrator IDs (bots excluded) not stored'
grep -Fqx "OAOS_PORTAL_TEAM_ID=$team" "$env_file" || fail 'team ID not stored'
[[ $(stat -c '%a' "$env_file") == 600 ]] || fail 'Hermes .env mode after the portal stage'
grep -Fq 'e-mail invitations are off' "$temp_root/stage2.out" || fail 'Mattermost invitation setting not reported'
unit="$stage_home/.config/systemd/user/oaos-portal.service"
grep -Fq "Environment=OAOS_PORTAL_PORT=$stage_port" "$unit" && grep -Fq 'Environment=OAOS_PORTAL_PUBLIC_URL=https://portal.example.com' "$unit" || fail 'user unit not rendered'
cmp -s "$server" "$stage_home/oaos/portal/bin/oaos-portal.py" || fail 'service copy not installed'
grep -Fq -- '--user enable oaos-portal.service' "$temp_root/systemctl.log" && grep -Fq -- '--user restart oaos-portal.service' "$temp_root/systemctl.log" || fail 'unit not enabled and restarted'
[[ -f $stage_home/oaos/portal/site.md ]] || fail 'starter site.md not created'
printf '# 운영자 문구\n\n문의: ops@example.com\n' > "$stage_home/oaos/portal/site.md"
: > "$temp_root/systemctl.log"
run_stage > "$temp_root/stage3.out" 2>&1 || fail 'portal stage rerun'
grep -Fq 'ops@example.com' "$sys_root/var/www/oaos-portal/index.html" || fail 'operator text not rendered into the managed page'
grep -Fq '운영자 문구' "$stage_home/oaos/portal/site.md" || fail 'rerun overwrote site.md'
grep -Fq -- '--user restart' "$temp_root/systemctl.log" && fail 'unchanged unit restarted'
printf '<html>hand-made</html>\n' > "$sys_root/var/www/oaos-portal/index.html"
run_stage > "$temp_root/stage4.out" 2>&1 || fail 'portal stage with an operator page'
grep -Fqx '<html>hand-made</html>' "$sys_root/var/www/oaos-portal/index.html" || fail 'an unmanaged page was replaced'
grep -Fq "$mm_token" "$temp_root"/stage*.out && fail 'bot token printed by the portal stage'
printf 'PASS: portal stage — ingress prerequisite, secret/admins/team in the Hermes .env, user unit, site.md kept, unmanaged page kept\n'
