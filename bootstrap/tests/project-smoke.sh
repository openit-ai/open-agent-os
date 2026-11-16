#!/usr/bin/env bash
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
for name in ${!OAOS_@}; do unset "$name"; done
unset HERMES_HOME SSH_CONNECTION
# Forks run the same smoke; the owner check itself is exercised explicitly below.
export OAOS_ALLOW_FORK=1
install="$repo_root/editions/project/install.sh"
verify="$repo_root/bootstrap/verify/project-verify.sh"
fail() {
  printf 'FAIL: %s\n' "$1" >&2
  for file in "$temp_root"/*.out "$temp_root"/*.err; do [[ -f $file ]] && { printf '\n--- %s ---\n' "$file" >&2; cat "$file" >&2; }; done
  exit 1
}
state_status() {
  python3 - "$1/.oaos-install/state.json" "$2" <<'PY'
import json, sys
state = json.load(open(sys.argv[1], encoding='utf-8'))
print(state['editions']['project']['stages'][sys.argv[2]]['status'])
PY
}

# Host privileges and service managers are always mocked: no test reaches real sudo,
# systemd or the user manager. sudo runs only the commands listed in OAOS_FAKE_SUDO_EXEC
# (logging every call) and reports no root access unless OAOS_FAKE_SUDO=1.
safe="$temp_root/safe-bin"
mkdir -p "$safe"
cat > "$safe/sudo" <<'MOCK'
#!/usr/bin/env bash
[[ $1 == -n ]] && shift
if [[ $1 == true ]]; then [[ ${OAOS_FAKE_SUDO:-0} == 1 ]]; exit; fi
printf '%s\n' "$*" >> "${OAOS_FAKE_SUDO_LOG:-/dev/null}"
[[ ${OAOS_FAKE_SUDO:-0} == 1 ]] || exit 1
case " ${OAOS_FAKE_SUDO_EXEC:-} " in *" $1 "*) exec "$@" ;; esac
exit 0
MOCK
cat > "$safe/runuser" <<'MOCK'
#!/usr/bin/env bash
while (($#)) && [[ $1 != -- ]]; do shift; done
shift
exec "$@"
MOCK
cat > "$safe/systemctl" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${OAOS_FAKE_SYSTEMCTL_LOG:-/dev/null}"
case $* in
  *is-enabled*certbot.timer*) [[ ${OAOS_FAKE_CERTBOT_TIMER:-0} == 1 ]] ;;
  *is-enabled*|*is-active*) exit 1 ;;
  *) exit 0 ;;
esac
MOCK
printf '#!/usr/bin/env bash\nprintf "no\\n"\n' > "$safe/loginctl"
chmod 700 "$safe"/*

mirror="$temp_root/no-jq-bin"
mkdir -p "$mirror"
for command_path in /usr/bin/*; do
  [[ -f $command_path || -L $command_path ]] || continue
  name=${command_path##*/}
  case $name in jq|sudo|systemctl|loginctl|runuser) ;; *) ln -s -- "$command_path" "$mirror/$name" ;; esac
done
[[ ! -e $mirror/jq && -x $mirror/python3 ]] || fail 'no-jq mirror invalid'
first="$temp_root/first"
mkdir -p "$first"
HOME="$first" PATH="$safe:$mirror" bash "$install" --stage wiki,harness >"$temp_root/first.out" 2>"$temp_root/first.err" || fail 'first install'
python3 - "$first/.oaos-install/state.json" <<'PY' || fail 'state schema'
import json,sys
x=json.load(open(sys.argv[1], encoding='utf-8'))
assert x['edition']=='project'
assert list(x['stages'])==['prep','hermes','llm','telegram','wiki','harness','cron','stack','ingress','mail','team','gateway','verify']
assert x['stages']['wiki']['status']==x['stages']['harness']['status']=='done'
assert x['editions']['project']['stages']['wiki']['status']=='done'
PY
HOME="$first" PATH="$safe:$mirror" bash "$install" --stage wiki,harness >"$temp_root/second.out" 2>"$temp_root/second.err" || fail 'rerun'
grep -Fq 'Skipping wiki (done).' "$temp_root/second.out" || fail 'wiki not skipped'
grep -Fq 'Skipping harness (done).' "$temp_root/second.out" || fail 'harness not skipped'
printf 'PASS: no-jq state and rerun\n'

# Edition-scoped state: Personal checkpoints never let Project skip its own stages.
scoped="$temp_root/scoped"
mkdir -p "$scoped/.oaos-install"
cat > "$scoped/.oaos-install/state.json" <<'JSON'
{"edition": "personal", "updated_at": "2026-01-01T00:00:00Z",
 "stages": {"prep": {"status": "done"}, "wiki": {"status": "done"}, "verify": {"status": "verified"}}}
JSON
HOME="$scoped" PATH="$safe:$PATH" bash "$install" --dry-run >"$temp_root/scoped.out" 2>"$temp_root/scoped.err" || fail 'scoped dry-run'
for stage in prep wiki verify; do grep -Fq "Would run $stage" "$temp_root/scoped.out" || fail "Personal state let Project skip $stage"; done
printf 'PASS: a Personal state does not let Project skip prep, wiki or verify\n'

# Repository owner check (architecture §6).
owner="$temp_root/owner"
mkdir -p "$owner"
if HOME="$owner" PATH="$safe:$PATH" OAOS_ALLOW_FORK=0 OAOS_TEST_MODE=1 OAOS_TEST_ORIGIN='https://github.com/someone/open-agent-os.git' \
  bash "$install" --stage wiki >"$temp_root/owner.out" 2>"$temp_root/owner.err"; then fail 'Project installer ran from a fork'; fi
[[ ! -e $owner/data/wiki ]] || fail 'fork run changed the host'
HOME="$owner" PATH="$safe:$PATH" OAOS_ALLOW_FORK=0 OAOS_TEST_MODE=1 OAOS_TEST_ORIGIN='git@github.com:openit-ai/open-agent-os.git' \
  bash "$install" --stage wiki >"$temp_root/owner2.out" 2>"$temp_root/owner2.err" || fail 'official origin refused'
printf 'PASS: Project installer refuses an unconfirmed fork\n'

# --- cron: the managed backup script --------------------------------------------------------
cron_home="$temp_root/cron-home"
cron_bin="$temp_root/cron-bin"
mkdir -p "$cron_home" "$cron_bin"
touch "$temp_root/cron-list"
cat > "$cron_bin/hermes" <<'MOCK'
#!/usr/bin/env bash
set -Eeuo pipefail
case "$1 $2" in
  'cron list') cat "$OAOS_FAKE_CRON_LIST" ;;
  'cron create')
    shift 2
    script=
    while (($#)); do case $1 in --script) script=$2; shift 2 ;; *) shift ;; esac; done
    [[ -n $script && $script != */* && $script != *\\* ]] || {
      printf 'Failed to create job: Script path must be relative to the Hermes scripts directory. Got: %s\n' "$script" >&2
      exit 1
    }
    printf '%s\n' "$script" >> "$OAOS_FAKE_CRON_STATE"
    printf 'oaos-daily-backup\n' >> "$OAOS_FAKE_CRON_LIST" ;;
  *) exit 1 ;;
esac
MOCK
chmod 700 "$cron_bin/hermes"
: > "$temp_root/cron-state"
run_cron() { HOME="$cron_home" PATH="$cron_bin:$safe:$PATH" OAOS_FAKE_CRON_LIST="$1" OAOS_FAKE_CRON_STATE="$temp_root/cron-state" bash "$install" --stage cron; }
run_cron "$temp_root/cron-list" >"$temp_root/cron.out" 2>"$temp_root/cron.err" || fail 'Project cron install'
[[ $(wc -l < "$temp_root/cron-list") == 1 ]] || fail 'Project cron registered unexpected jobs'
grep -q '/' "$temp_root/cron-state" && fail 'Project cron passed an absolute script path'
cmp -s "$repo_root/editions/project/backup.sh" "$cron_home/.hermes/scripts/oaos-daily-backup.sh" || fail 'cron did not install the managed backup script'
[[ $(stat -c '%a' "$cron_home/.oaos/backups") == 700 ]] || fail 'backup directory is not 700'
rm -rf "$cron_home/.oaos-install"
run_cron "$temp_root/cron-list" >"$temp_root/cron-rerun.out" 2>"$temp_root/cron-rerun.err" || fail 'Project cron rerun'
[[ $(wc -l < "$temp_root/cron-list") == 1 ]] || fail 'Project cron duplicated the backup job'
# The script from earlier releases is upgraded in place.
python3 - "$install" "$cron_home/.hermes/scripts/oaos-daily-backup.sh" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
legacy = re.search(r"legacy_backup_script\(\) \{\n  cat <<'EOF'\n(.*?)\nEOF\n", text, re.S).group(1) + "\n"
open(sys.argv[2], "w").write(legacy)
PY
rm -rf "$cron_home/.oaos-install"
run_cron "$temp_root/cron-list" >"$temp_root/cron-legacy.out" 2>"$temp_root/cron-legacy.err" || fail 'legacy backup script was not upgraded'
cmp -s "$repo_root/editions/project/backup.sh" "$cron_home/.hermes/scripts/oaos-daily-backup.sh" || fail 'legacy backup script left in place'
# An operator-modified script is preserved instead of overwritten.
printf '#!/usr/bin/env bash\necho operator\n' > "$cron_home/.hermes/scripts/oaos-daily-backup.sh"
rm -rf "$cron_home/.oaos-install"
if run_cron "$temp_root/cron-list" >"$temp_root/cron-preserve.out" 2>"$temp_root/cron-preserve.err"; then
  fail 'operator-modified cron script was overwritten'
fi
grep -Fq 'echo operator' "$cron_home/.hermes/scripts/oaos-daily-backup.sh" || fail 'operator cron script was not preserved'
printf 'PASS: Project cron installs the managed backup script once, upgrades the legacy one, preserves operator edits\n'

# --- backup and restore dry run ---------------------------------------------------------------
bk_home="$temp_root/bk-home"
bk_sys="$temp_root/bk-sys"
bk_bin="$temp_root/bk-bin"
mkdir -p "$bk_home/.hermes" "$bk_home/data/wiki" "$bk_home/oaos/stack" "$bk_bin" \
  "$bk_sys/etc/oaos" "$bk_sys/opt/mattermost/config" "$bk_sys/opt/mattermost/data" "$bk_sys/var/lib/outline/data"
printf 'X=1\n' > "$bk_home/.hermes/.env"
printf 'MM_DB_PASSWORD=x\n' > "$bk_home/oaos/stack/.env"
printf 'URL=x\n' > "$bk_sys/etc/oaos/outline.env"
printf '{}\n' > "$bk_sys/opt/mattermost/config/config.json"
printf 'file\n' > "$bk_sys/opt/mattermost/data/a"
printf 'file\n' > "$bk_sys/var/lib/outline/data/b"
printf '#!/usr/bin/env bash\nprintf "PGDMP-fake-%%s\\n" "${@: -1}"\n' > "$bk_bin/pg_dump"
printf '#!/usr/bin/env bash\n[[ $1 == --list ]] && grep -q "^PGDMP" "$2"\n' > "$bk_bin/pg_restore"
chmod 700 "$bk_bin"/*
run_backup() { HOME="$bk_home" PATH="$bk_bin:$safe:$PATH" OAOS_FAKE_SUDO=1 OAOS_FAKE_SUDO_EXEC='runuser test tar' \
  OAOS_TEST_MODE=1 OAOS_TEST_SYSTEM_ROOT="$bk_sys" OAOS_BACKUP_KEEP=2 bash "$repo_root/editions/project/backup.sh"; }
mkdir -p "$bk_home/.oaos/backups"
for old in 20200101-000000 20200102-000000; do
  touch -d '2020-01-02' "$bk_home/.oaos/backups/oaos-project-$old.tar"
done
archive=$(run_backup 2>"$temp_root/backup.err" | tail -n 1) || fail 'backup job failed'
[[ -f $archive && $(stat -c '%a' "$archive") == 600 && $(stat -c '%a' "$bk_home/.oaos/backups") == 700 ]] || fail 'backup archive or directory mode wrong'
manifest=$(tar -xOf "$archive" ./manifest.txt)
for item in 'database mattermost' 'database outline' 'system etc/oaos opt/mattermost/config/config.json opt/mattermost/data var/lib/outline/data' 'home .hermes data/wiki oaos/stack'; do
  grep -Fq "$item" <<< "$manifest" || fail "backup manifest lacks: $item"
done
[[ $(find "$bk_home/.oaos/backups" -name 'oaos-project-*.tar' | wc -l) == 2 ]] || fail 'backup rotation did not keep the newest archives'
HOME="$bk_home" PATH="$bk_bin:$safe:$PATH" bash "$repo_root/editions/project/restore.sh" --dry-run "$archive" >"$temp_root/restore.out" 2>"$temp_root/restore.err" || fail 'restore dry run rejected a good archive'
grep -Fq 'Would restore database outline' "$temp_root/restore.out" || fail 'restore dry run did not list the databases'
grep -Fq 'nothing was changed' "$temp_root/restore.out" || fail 'restore dry run did not confirm no change'
bad="$temp_root/bad.tar"
mkdir -p "$temp_root/bad"
tar -xf "$archive" -C "$temp_root/bad"
printf 'broken\n' > "$temp_root/bad/outline.dump"
tar -cf "$bad" -C "$temp_root/bad" .
if HOME="$bk_home" PATH="$bk_bin:$safe:$PATH" bash "$repo_root/editions/project/restore.sh" --dry-run "$bad" >"$temp_root/restore-bad.out" 2>"$temp_root/restore-bad.err"; then
  fail 'restore dry run accepted a damaged dump'
fi
rm -rf "$bk_sys/var/lib/outline/data"
if run_backup >"$temp_root/backup-missing.out" 2>"$temp_root/backup-missing.err"; then fail 'backup succeeded without Outline data'; fi
printf 'PASS: backup captures PostgreSQL, service data, /etc/oaos and stack (600/700, rotation); restore dry run checks every part\n'

# --- prep: G0 capacity, port conflicts, UFW ordering --------------------------------------------
prep_bin="$temp_root/prep-bin"
mkdir -p "$prep_bin"
cat > "$prep_bin/ufw" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$OAOS_FAKE_UFW_LOG"
[[ $1 == status ]] && printf 'Status: inactive\n'
exit 0
MOCK
cat > "$prep_bin/ss" <<'MOCK'
#!/usr/bin/env bash
[[ -n ${OAOS_FAKE_PORT:-} && $* == *":$OAOS_FAKE_PORT"* ]] && printf 'LISTEN 0 4096 0.0.0.0:%s 0.0.0.0:*\n' "$OAOS_FAKE_PORT"
exit 0
MOCK
printf '#!/usr/bin/env bash\nexit 1\n' > "$prep_bin/dpkg-query"
printf '#!/usr/bin/env bash\nexit 0\n' > "$prep_bin/apt-get"
chmod 700 "$prep_bin"/*
run_prep() {
  local home=$1; shift
  mkdir -p "$home"
  : > "$temp_root/ufw.log"
  env HOME="$home" PATH="$prep_bin:$safe:$PATH" OAOS_FAKE_SUDO=1 OAOS_FAKE_SUDO_EXEC='ufw' OAOS_FAKE_UFW_LOG="$temp_root/ufw.log" \
    OAOS_TEST_MODE=1 "$@" bash "$install" --stage prep
}
code=0
run_prep "$temp_root/prep-small" OAOS_TEST_HOST_FACTS='1 4000000 30000000' >"$temp_root/prep-small.out" 2>"$temp_root/prep-small.err" || code=$?
[[ $code == 3 ]] || fail "undersized host did not block prep (exit $code)"
grep -Fq 'G0 BLOCKED' "$temp_root/prep-small.err" || fail 'undersized host not reported'
run_prep "$temp_root/prep-warn" OAOS_TEST_HOST_FACTS='2 8000000 100000000' >"$temp_root/prep-warn.out" 2>"$temp_root/prep-warn.err" || true
grep -Fq 'G0 WARN: 2 vCPU' "$temp_root/prep-warn.err" || fail 'below-recommendation host not warned'
grep -Fq 'G0 BLOCKED' "$temp_root/prep-warn.err" && fail 'below-recommendation host was blocked'
code=0
run_prep "$temp_root/prep-port" OAOS_TEST_HOST_FACTS='4 16000000 200000000' OAOS_FAKE_PORT=8065 >"$temp_root/prep-port.out" 2>"$temp_root/prep-port.err" || code=$?
[[ $code == 3 ]] || fail 'port conflict did not block prep'
grep -Fq '8065 (expected mattermost)' "$temp_root/prep-port.err" || fail 'port conflict not named'
run_prep "$temp_root/prep-ufw" OAOS_TEST_HOST_FACTS='4 16000000 200000000' SSH_CONNECTION='198.51.100.2 50000 203.0.113.10 2222' \
  >"$temp_root/prep-ufw.out" 2>"$temp_root/prep-ufw.err" || true
[[ $(cat "$temp_root/ufw.log") == $'allow 22/tcp\nallow 2222/tcp\nallow 80/tcp\nallow 443/tcp\nstatus\n--force enable' ]] ||
  fail "UFW was not enabled after the SSH, 80 and 443 rules: $(tr '\n' ';' < "$temp_root/ufw.log")"
printf 'PASS: G0 prep blocks undersized hosts and port conflicts, warns below recommendation, enables UFW only after SSH/80/443 rules\n'

# --- gateway stage ---------------------------------------------------------------------------
gateway_bin="$temp_root/gateway-bin"
mkdir -p "$gateway_bin"
cat > "$gateway_bin/hermes" <<'MOCK'
#!/usr/bin/env bash
set -Eeuo pipefail
case "$1 $2" in
  'config get') printf 'deny\n' ;;
  'gateway status')
    if [[ -f $OAOS_FAKE_GATEWAY_MARKER ]]; then
      case ${OAOS_FAKE_GATEWAY_MODE:-} in
        starts) printf '✓ Gateway is running (PID: 4242)\n' ;;
        starts_service) printf '✓ User gateway service is running\n' ;;
        *) printf '✗ Gateway is not running\n' ;;
      esac
    else
      printf '✗ Gateway is not running\n'
    fi ;;
  'gateway install')
    [[ ${3:-} == --start-on-login && ${4:-} == --start-now ]] || exit 1
    : > "$OAOS_FAKE_GATEWAY_MARKER"
    printf 'install\n' >> "${OAOS_FAKE_GATEWAY_INSTALL_LOG:-/dev/null}" ;;
  *) exit 1 ;;
esac
MOCK
cat > "$gateway_bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
[[ $* == '--user is-enabled hermes-gateway' ]]
MOCK
printf '#!/usr/bin/env bash\nprintf "yes\\n"\n' > "$gateway_bin/loginctl"
chmod 700 "$gateway_bin"/*
telegram_done() {
  mkdir -p "$1/.oaos-install"
  printf '{"edition":"project","stages":{"telegram":{"status":"done"}}}\n' > "$1/.oaos-install/state.json"
}
gateway_down="$temp_root/gateway-down"
telegram_done "$gateway_down"
if HOME="$gateway_down" PATH="$gateway_bin:$safe:$PATH" OAOS_FAKE_GATEWAY_MARKER="$gateway_down/installed" OAOS_FAKE_GATEWAY_MODE=stays_down \
  bash "$install" --stage gateway >"$temp_root/gateway-down.out" 2>"$temp_root/gateway-down.err"; then
  fail 'not-running gateway was marked done'
fi
[[ -f $gateway_down/installed ]] || fail 'gateway install was skipped on a new host'
[[ $(state_status "$gateway_down" gateway) == blocked ]] || fail 'not-running gateway state was not blocked'
grep -Fq 'gateway done.' "$temp_root/gateway-down.out" && fail 'not-running gateway logged success'
gateway_wait="$temp_root/gateway-wait"
mkdir -p "$gateway_wait"
if HOME="$gateway_wait" PATH="$gateway_bin:$safe:$PATH" OAOS_FAKE_GATEWAY_MARKER="$gateway_wait/installed" OAOS_FAKE_GATEWAY_MODE=starts \
  bash "$install" --stage gateway >"$temp_root/gateway-wait.out" 2>"$temp_root/gateway-wait.err"; then
  fail 'gateway started before telegram was done'
fi
[[ ! -f $gateway_wait/installed ]] || fail 'gateway installed before telegram was done'
gateway_up="$temp_root/gateway-up"
telegram_done "$gateway_up"
HOME="$gateway_up" PATH="$gateway_bin:$safe:$PATH" OAOS_FAKE_GATEWAY_MARKER="$gateway_up/installed" OAOS_FAKE_GATEWAY_MODE=starts \
  bash "$install" --stage gateway >"$temp_root/gateway-up.out" 2>"$temp_root/gateway-up.err" || fail 'gateway did not complete after install'
[[ -f $gateway_up/installed ]] || fail 'gateway install command was not called'
[[ $(state_status "$gateway_up" gateway) == 'done' ]] || fail 'running gateway state was not done'
gateway_pre="$temp_root/gateway-pre"
telegram_done "$gateway_pre"
touch "$gateway_pre/installed"
: > "$temp_root/gateway-install.log"
HOME="$gateway_pre" PATH="$gateway_bin:$safe:$PATH" OAOS_FAKE_GATEWAY_MARKER="$gateway_pre/installed" OAOS_FAKE_GATEWAY_MODE=starts_service \
  OAOS_FAKE_GATEWAY_INSTALL_LOG="$temp_root/gateway-install.log" \
  bash "$install" --stage gateway >"$temp_root/gateway-pre.out" 2>"$temp_root/gateway-pre.err" || fail 'already-running service did not complete the gateway stage'
[[ ! -s $temp_root/gateway-install.log ]] || fail 'gateway install reran although the service was already running'
printf 'PASS: gateway waits for telegram, installs on a new host, blocks when not running, skips install when running\n'

# --- team: G7 bot principle, G8 agent account, secrets off argv, restart only when running ----
team_bin="$temp_root/team-bin"
mkdir -p "$team_bin"
cat > "$team_bin/curl" <<'MOCK'
#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
with open(os.environ['OAOS_FAKE_ARGV_LOG'], 'a') as log:
    log.write('curl ' + ' '.join(args) + '\n')
if '-K' not in args:
    sys.exit(7)
config = sys.stdin.read()
url = re.search(r'^url = "([^"]+)"', config, re.M).group(1)
mode = os.environ.get('OAOS_FAKE_MODE', 'good')
if url.endswith('/api/v4/users/me'):
    body = {'id': 'b' * 26, 'is_bot': mode != 'human',
            'roles': 'system_user system_admin' if mode == 'admin' else 'system_user'}
elif url.endswith('/api/v4/users/ids'):
    ids = json.loads(re.search(r'^data = "(.*)"$', config, re.M).group(1).replace('\\"', '"'))
    body = [{'id': i} for i in ids if i != 'z' * 26]
elif url.endswith('/api/auth.info'):
    body = {'data': {'user': {'email': 'other@example.com' if mode == 'outline-wrong' else 'Bot@Example.com',
                              'role': 'admin' if mode == 'outline-admin' else 'member'}}}
else:
    sys.exit(7)
sys.stdout.write(json.dumps(body) + '\n200')
MOCK
cat > "$team_bin/hermes" <<'MOCK'
#!/usr/bin/env bash
printf 'hermes %s\n' "$*" >> "$OAOS_FAKE_ARGV_LOG"
case "$1 $2" in
  'gateway status') [[ ${OAOS_FAKE_GW:-} == running ]] && printf '✓ Gateway is running\n' || printf '✗ Gateway is not running\n' ;;
  'gateway restart') exit 0 ;;
  *) exit 1 ;;
esac
MOCK
printf '#!/usr/bin/env bash\nprintf %s "[{\\"id\\":\\"admin\\"}]"\n' "'%s'" > "$team_bin/mmctl"
chmod 700 "$team_bin"/*
mm_token='mmbottoken0123456789abcdef'
ol_token='ol_api_agenttoken0123456789abcdefghijklmnopqr'
allowed=$(printf 'a%.0s' {1..26})
team_setup() {
  local home=$1
  rm -rf "$home"
  mkdir -p "$home/oaos/stack" "$home/.hermes"
  chmod 700 "$home/.hermes"
  printf 'CHAT_DOMAIN=chat.example.com\nNOTE_DOMAIN=note.example.com\nPORTAL_DOMAIN=portal.example.com\nMATTERMOST_TOKEN=%s\n' "$mm_token" > "$home/oaos/stack/.env"
  printf 'MATTERMOST_ALLOWED_USERS=%s\nOAOS_MAIL_ADDRESS=bot@example.com\nOUTLINE_API_TOKEN=%s\n' "$allowed" "$ol_token" > "$home/.hermes/.env"
  chmod 600 "$home/oaos/stack/.env" "$home/.hermes/.env"
}
run_team() {
  local home=$1; shift
  env HOME="$home" PATH="$team_bin:$safe:$PATH" OAOS_FAKE_SUDO=1 OAOS_FAKE_SUDO_EXEC='runuser' OAOS_TEST_MODE=1 \
    OAOS_TEST_MMCTL="$team_bin/mmctl" OAOS_FAKE_ARGV_LOG="$temp_root/argv.log" "$@" bash "$install" --stage team
}
team_home="$temp_root/team-home"
: > "$temp_root/argv.log"
team_setup "$team_home"
run_team "$team_home" >"$temp_root/team.out" 2>"$temp_root/team.err" || fail 'team did not complete on a fresh host without a gateway unit'
grep -Fq 'gateway restart' "$temp_root/argv.log" && fail 'team restarted a gateway that is not installed'
grep -Fq 'MATTERMOST_TOKEN' "$team_home/oaos/stack/.env" && fail 'Mattermost token copy left in stack/.env'
for line in "MATTERMOST_TOKEN=$mm_token" 'MATTERMOST_URL=https://chat.example.com' 'OUTLINE_URL=https://note.example.com'; do
  grep -Fqx "$line" "$team_home/.hermes/.env" || fail "Hermes .env lacks ${line%%=*}"
done
[[ $(stat -c '%a' "$team_home/.hermes/.env") == 600 ]] || fail 'Hermes .env lost mode 600'
grep -Fq -e "$mm_token" -e "$ol_token" "$temp_root/argv.log" && fail 'a token reached a command argument'
[[ -f $team_home/oaos/team-onboarding.md ]] || fail 'team onboarding note missing'
mkdir -p "$team_home/.config/systemd/user"
touch "$team_home/.config/systemd/user/hermes-gateway.service"
rm -rf "$team_home/.oaos-install"
run_team "$team_home" OAOS_FAKE_GW=running >"$temp_root/team-restart.out" 2>"$temp_root/team-restart.err" || fail 'team rerun with a running gateway'
grep -Fq 'hermes gateway restart' "$temp_root/argv.log" || fail 'team did not restart a running gateway'
expect_team_block() {
  local label=$1 message=$2 code=0; shift 2
  team_setup "$team_home"
  "$@"
  run_team "$team_home" ${OAOS_CASE:+OAOS_FAKE_MODE=$OAOS_CASE} >"$temp_root/team-$label.out" 2>"$temp_root/team-$label.err" || code=$?
  [[ $code == 3 ]] || fail "team $label was not blocked (exit $code)"
  grep -Fq "$message" "$temp_root/team-$label.err" || fail "team $label message missing"
}
OAOS_CASE='human' expect_team_block human 'human account, not a bot' true
OAOS_CASE='admin' expect_team_block admin 'bot account has the system_admin role' true
OAOS_CASE='outline-wrong' expect_team_block outline-wrong 'other than the G9 bot mailbox' true
OAOS_CASE='outline-admin' expect_team_block outline-admin 'workspace admin' true
OAOS_CASE='' expect_team_block allowlist 'Mattermost user IDs allowed' sed -i '/^MATTERMOST_ALLOWED_USERS=/d' "$team_home/.hermes/.env"
OAOS_CASE='' expect_team_block unknown-id 'allowlisted ID(s) not found' sed -i "s/^MATTERMOST_ALLOWED_USERS=.*/MATTERMOST_ALLOWED_USERS=$(printf 'z%.0s' {1..26})/" "$team_home/.hermes/.env"
OAOS_CASE='' expect_team_block no-mailbox 'G8 needs the G9 bot mailbox' sed -i '/^OAOS_MAIL_ADDRESS=/d' "$team_home/.hermes/.env"
printf 'PASS: team accepts only a non-admin Mattermost bot and the Outline agent account, needs the allowlist, keeps tokens off argv, restarts only a running gateway\n'

# --- ingress: DNS polling, ACME e-mail default, managed vhost re-render ----------------------
ing_bin="$temp_root/ing-bin"
ing_sys="$temp_root/ing-sys"
mkdir -p "$ing_bin"
cat > "$ing_bin/curl" <<'MOCK'
#!/usr/bin/env bash
[[ $* == *api.ipify.org* ]] && { printf '203.0.113.10'; exit 0; }
exit 7
MOCK
cat > "$ing_bin/getent" <<'MOCK'
#!/usr/bin/env bash
[[ ${OAOS_FAKE_DNS:-} == ok ]] && printf '203.0.113.10 STREAM %s\n' "$2" || printf '198.51.100.1 STREAM %s\n' "$2"
MOCK
printf '#!/usr/bin/env bash\nexit 0\n' > "$ing_bin/nginx"
cat > "$ing_bin/openssl" <<'MOCK'
#!/usr/bin/env bash
[[ -f ${*: -1} ]]
MOCK
cat > "$ing_bin/certbot" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$OAOS_FAKE_CERTBOT_LOG"
while (($#)); do [[ $1 == -d ]] && domain=$2; shift; done
mkdir -p "$OAOS_TEST_SYSTEM_ROOT/etc/letsencrypt/live/$domain"
: > "$OAOS_TEST_SYSTEM_ROOT/etc/letsencrypt/live/$domain/fullchain.pem"
MOCK
chmod 700 "$ing_bin"/*
ing_home="$temp_root/ing-home"
mkdir -p "$ing_home/oaos/stack" "$ing_home/.hermes" "$ing_sys"
chmod 700 "$ing_home/.hermes"
printf 'CHAT_DOMAIN=chat.example.com\nNOTE_DOMAIN=note.example.com\nPORTAL_DOMAIN=portal.example.com\n' > "$ing_home/oaos/stack/.env"
printf 'OAOS_MAIL_ADDRESS=bot@example.com\n' > "$ing_home/.hermes/.env"
chmod 600 "$ing_home/.hermes/.env" "$ing_home/oaos/stack/.env"
run_ingress() {
  env HOME="$ing_home" PATH="$ing_bin:$safe:$PATH" OAOS_FAKE_SUDO=1 OAOS_FAKE_SUDO_EXEC='install cat tee ln nginx openssl certbot systemctl' \
    OAOS_TEST_MODE=1 OAOS_TEST_SYSTEM_ROOT="$ing_sys" OAOS_FAKE_CERTBOT_TIMER=1 OAOS_FAKE_CERTBOT_LOG="$temp_root/certbot.log" \
    OAOS_DNS_TIMEOUT=0 "$@" bash "$install" --recheck --stage ingress
}
: > "$temp_root/certbot.log"
code=0
run_ingress >"$temp_root/ing-dns.out" 2>"$temp_root/ing-dns.err" || code=$?
[[ $code == 3 ]] || fail 'ingress did not block on wrong DNS'
grep -Fq 'still do not resolve to this server' "$temp_root/ing-dns.err" || fail 'DNS timeout not reported'
grep -Fq 'no certificate e-mail' "$temp_root/ing-dns.err" && fail 'ACME e-mail did not default to the G9 address'
run_ingress OAOS_FAKE_DNS=ok >"$temp_root/ing.out" 2>"$temp_root/ing.err" || fail 'ingress with matching DNS'
[[ $(grep -c -- '--email bot@example.com' "$temp_root/certbot.log") == 3 ]] || fail 'certbot was not run once per domain with the G9 e-mail'
for name in chat note portal; do grep -Fq '# OAOS template sha256:' "$ing_sys/etc/nginx/sites-available/oaos-$name.conf" || fail "vhost $name lacks the template hash"; done
printf '    listen 443 ssl; # managed by Certbot\n' >> "$ing_sys/etc/nginx/sites-available/oaos-note.conf"
: > "$temp_root/certbot.log"
run_ingress OAOS_FAKE_DNS=ok >"$temp_root/ing-rerun.out" 2>"$temp_root/ing-rerun.err" || fail 'ingress rerun'
[[ ! -s $temp_root/certbot.log ]] || fail 'unchanged ingress reran certbot'
grep -Fq 'managed by Certbot' "$ing_sys/etc/nginx/sites-available/oaos-note.conf" || fail 'unchanged vhost lost the certbot TLS lines'
sed -i 's/^CHAT_DOMAIN=.*/CHAT_DOMAIN=team.example.com/' "$ing_home/oaos/stack/.env"
run_ingress OAOS_FAKE_DNS=ok >"$temp_root/ing-change.out" 2>"$temp_root/ing-change.err" || fail 'ingress after a domain change'
grep -Fq 'server_name team.example.com;' "$ing_sys/etc/nginx/sites-available/oaos-chat.conf" || fail 'chat vhost was not re-rendered for the new domain'
[[ $(cat "$temp_root/certbot.log") == *'-d team.example.com'* && $(wc -l < "$temp_root/certbot.log") == 1 ]] || fail 'certbot did not run for exactly the changed domain'
grep -Fq 'https://team.example.com' "$ing_sys/var/www/oaos-portal/index.html" || fail 'managed portal page was not refreshed'
printf 'PASS: ingress polls DNS with a timeout, defaults the ACME e-mail to G9, re-renders only changed managed vhosts\n'

# --- mail: legacy secret move, Gmail defaults, login failure blocks -------------------------
mail_home="$temp_root/mail-home"
mkdir -p "$mail_home/oaos/stack" "$mail_home/.hermes"
chmod 700 "$mail_home/.hermes"
printf 'NOTE_DOMAIN=note.example.com\nOAOS_MAIL_PASSWORD=abcdefghijklmnop\n' > "$mail_home/oaos/stack/.env"
printf 'OAOS_MAIL_ADDRESS=bot@example.com\nOAOS_MAIL_IMAP_HOST=127.0.0.1\nOAOS_MAIL_SMTP_HOST=127.0.0.1\n' > "$mail_home/.hermes/.env"
chmod 600 "$mail_home/oaos/stack/.env" "$mail_home/.hermes/.env"
code=0
HOME="$mail_home" PATH="$safe:$PATH" OAOS_MAIL_TIMEOUT=3 OAOS_MAIL_PASSWORD=from-argv bash "$install" --stage mail >"$temp_root/mail.out" 2>"$temp_root/mail.err" || code=$?
[[ $code == 3 ]] || fail "mail with a failing IMAP login was not blocked (exit $code)"
grep -Fq 'IMAP login to 127.0.0.1 failed' "$temp_root/mail.err" || fail 'IMAP failure not reported'
grep -Fq 'OAOS_MAIL_PASSWORD in the process environment is ignored' "$temp_root/mail.err" || fail 'environment secret was not refused'
grep -Fq 'OAOS_MAIL_PASSWORD' "$mail_home/oaos/stack/.env" && fail 'mail password left in stack/.env'
grep -Fqx 'OAOS_MAIL_PASSWORD=abcdefghijklmnop' "$mail_home/.hermes/.env" || fail 'mail password not moved into the Hermes .env'
(
  # shellcheck source=bootstrap/lib/common.sh
  . "$repo_root/bootstrap/lib/common.sh"
  # shellcheck source=bootstrap/lib/platform.sh
  . "$repo_root/bootstrap/lib/platform.sh"
  # shellcheck source=bootstrap/lib/secret.sh
  . "$repo_root/bootstrap/lib/secret.sh"
  # shellcheck source=editions/project/lib.sh
  . "$repo_root/editions/project/lib.sh"
  [[ $(project_mail_default_host imap Bot@GMail.com) == imap.gmail.com && $(project_mail_default_host smtp x@googlemail.com) == smtp.gmail.com ]] || exit 1
  ! project_mail_default_host imap bot@example.com >/dev/null
) || fail 'Gmail IMAP/SMTP defaults'
printf 'PASS: mail moves legacy secrets into the Hermes .env, refuses env secrets, defaults Gmail hosts, blocks on login failure\n'

# --- Outline environment: OAOS keys rewritten, IdP keys mapped, operator keys preserved -------
oe="$temp_root/outline-env"
mkdir -p "$oe"
printf 'NOTE_DOMAIN=note.example.com\nOUTLINE_DB_PASSWORD=p\nOUTLINE_SECRET_KEY=s\nOUTLINE_UTILS_SECRET=u\n' > "$oe/stack"
printf 'OAOS_MAIL_ADDRESS=bot@example.com\nOAOS_MAIL_PASSWORD=pw\nOAOS_MAIL_SMTP_HOST=smtp.example.com\nOUTLINE_OIDC_CLIENT_ID=cid\nOUTLINE_API_TOKEN=x\n' > "$oe/hermes"
printf 'URL=https://old.example.com\nSLACK_CLIENT_ID=operator\nSMTP_HOST=stale\n' > "$oe/out"
[[ $(python3 "$repo_root/editions/project/render-outline-env.py" "$oe/stack" "$oe/hermes" "$oe/out") == changed ]] || fail 'outline.env not rendered'
for line in URL=https://note.example.com SLACK_CLIENT_ID=operator OIDC_CLIENT_ID=cid SMTP_HOST=smtp.example.com SMTP_PASSWORD=pw; do
  grep -Fqx "$line" "$oe/out" || fail "outline.env lacks $line"
done
grep -Fq 'API_TOKEN' "$oe/out" && fail 'outline.env received a non-IdP key'
[[ $(stat -c '%a' "$oe/out") == 600 ]] || fail 'outline.env is not 600'
[[ $(python3 "$repo_root/editions/project/render-outline-env.py" "$oe/stack" "$oe/hermes" "$oe/out") == unchanged ]] || fail 'outline.env render not idempotent'
printf 'PASS: outline.env keeps operator IdP keys, maps OUTLINE_OIDC_* and drops nothing else\n'

# --- verify stage exit semantics ---------------------------------------------------------------
verify_repo="$temp_root/verify-repo"
mkdir -p "$verify_repo/editions/project" "$verify_repo/bootstrap"
cp -R "$repo_root/editions/project/." "$verify_repo/editions/project/"
cp -R "$repo_root/bootstrap/lib" "$verify_repo/bootstrap/lib"
mkdir -p "$verify_repo/bootstrap/verify"
for case_code in 0 2 1; do
  printf '#!/usr/bin/env bash\nprintf "Summary: mocked\\n"\nexit %s\n' "$case_code" > "$verify_repo/bootstrap/verify/project-verify.sh"
  verify_home="$temp_root/verify-home-$case_code"
  mkdir -p "$verify_home"
  code=0
  HOME="$verify_home" PATH="$safe:$PATH" bash "$verify_repo/editions/project/install.sh" --stage verify >"$temp_root/verify-$case_code.out" 2>"$temp_root/verify-$case_code.err" || code=$?
  [[ $code == "$case_code" ]] || fail "verify exit $case_code became installer exit $code"
  case $case_code in 0) want=verified ;; 2) want=applied ;; 1) want=failed ;; esac
  [[ $(state_status "$verify_home" verify) == "$want" ]] || fail "verify exit $case_code was not recorded as $want"
done
printf 'PASS: verify stage records verified / applied (MANUAL pending, exit 2) / failed (exit 1)\n'

# --- project-verify.sh ---------------------------------------------------------------------------
verify_perm_home="$temp_root/verify-perm-home"
verify_perm_bin="$temp_root/verify-perm-bin"
mkdir -p "$verify_perm_home/oaos/stack" "$verify_perm_home/.config/systemd/user" "$verify_perm_home/.hermes" "$verify_perm_bin"
chmod 700 "$verify_perm_home/.hermes"
touch "$verify_perm_home/.config/systemd/user/hermes-gateway.service"
printf 'CHAT_DOMAIN=chat.example.invalid\nNOTE_DOMAIN=note.example.invalid\nPORTAL_DOMAIN=portal.example.invalid\n' > "$verify_perm_home/oaos/stack/.env"
cat > "$verify_perm_bin/sudo" <<'MOCK'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ $1 == -n ]] && shift
[[ $1 == true ]] && exit 0
printf '%s\n' "$1" >> "$OAOS_FAKE_SUDO_LOG"
case $1 in openssl|nginx) "$@" ;; *) exit 1 ;; esac
MOCK
printf '#!/usr/bin/env bash\n[[ $1 == x509 ]]\n' > "$verify_perm_bin/openssl"
printf '#!/usr/bin/env bash\n[[ $1 == -t ]]\n' > "$verify_perm_bin/nginx"
cat > "$verify_perm_bin/hermes" <<'MOCK'
#!/usr/bin/env bash
if [[ ${1:-} == gateway && ${2:-} == status ]]; then
  if [[ ${OAOS_FAKE_GW_STATE:-} == running_service ]]; then printf '✓ User gateway service is running\n'
  else printf '✗ Gateway is not running\n'; fi
  exit 0
fi
exit 1
MOCK
printf '#!/usr/bin/env bash\nexit 1\n' > "$verify_perm_bin/curl"
printf '#!/usr/bin/env bash\nexit 0\n' > "$verify_perm_bin/ss"
chmod 700 "$verify_perm_bin"/*
run_verify() { env HOME="$verify_perm_home" PATH="$verify_perm_bin:$safe:$PATH" OAOS_FAKE_SUDO_LOG="$temp_root/sudo.log" OAOS_VERIFY_RETRY_SLEEP=0 "$@"; }
: > "$temp_root/sudo.log"
if run_verify bash "$verify" --offline >"$temp_root/verify-perm.out" 2>"$temp_root/verify-perm.err"; then
  fail 'incomplete Project host verified successfully'
fi
grep -Eq 'TLS CHAT_DOMAIN[[:space:]]+PASS' "$temp_root/verify-perm.out" || fail 'non-root TLS check did not use sudo'
grep -Eq 'nginx config[[:space:]]+PASS' "$temp_root/verify-perm.out" || fail 'non-root nginx check did not use sudo'
grep -Eq 'Gateway service[[:space:]]+FAIL' "$temp_root/verify-perm.out" || fail 'not-running gateway verified as healthy'
[[ $(grep -Fcx openssl "$temp_root/sudo.log") == 3 && $(grep -Fcx nginx "$temp_root/sudo.log") == 1 ]] || fail 'privileged verifier checks were not routed through sudo'
for row in 'Unattended approvals' 'Firewall' 'Loopback :5432' 'Loopback :6379' 'Loopback :8065' 'Loopback :3000' 'PostgreSQL bind' \
  'Service secret files' 'Mattermost bot' 'Mattermost allowlist' 'Outline agent token' 'Outline URL' 'Gateway mattermost' 'Backup works' 'Restore dry run'; do
  grep -Eq "^[0-9]+ +$row +FAIL" "$temp_root/verify-perm.out" || fail "verify row '$row' is not FAIL on an empty host"
done
for row in 'Mattermost chat' 'Mattermost deny' 'Outline page' 'Mail flow' 'Reboot survival'; do
  grep -Eq "^[0-9]+ +$row +MANUAL" "$temp_root/verify-perm.out" || fail "verify row '$row' is not MANUAL"
done
OAOS_FAKE_GW_STATE=running_service run_verify bash "$verify" --offline --confirm mm-chat >"$temp_root/verify-perm-up.out" 2>"$temp_root/verify-perm-up.err" || true
grep -Eq 'Gateway service[[:space:]]+PASS' "$temp_root/verify-perm-up.out" || fail 'service-mode gateway was not verified as running'
grep -Eq 'Mattermost chat +PASS +Owner confirmed' "$temp_root/verify-perm-up.out" || fail '--confirm mm-chat not recorded'
printf 'PASS: verify routes root reads through sudo and reports Project rows (approvals, UFW, loopback, bots, backup) as FAIL/MANUAL\n'

# A G9 login that fails while the values exist is FAIL, not MANUAL.
printf 'OAOS_MAIL_ADDRESS=bot@example.com\nOAOS_MAIL_PASSWORD=abcdefghijklmnop\nOAOS_MAIL_IMAP_HOST=127.0.0.1\nOAOS_MAIL_SMTP_HOST=127.0.0.1\n' > "$verify_perm_home/.hermes/.env"
chmod 600 "$verify_perm_home/.hermes/.env"
code=0
run_verify OAOS_MAIL_TIMEOUT=3 bash "$verify" >"$temp_root/verify-mail.out" 2>"$temp_root/verify-mail.err" || code=$?
[[ $code == 1 ]] || fail "verify with a failing G9 login exited $code"
grep -Eq 'Bot mailbox IMAP +FAIL +IMAP login failed' "$temp_root/verify-mail.out" || fail 'G9 IMAP login failure was not FAIL'
grep -Eq 'Bot mailbox SMTP +FAIL +SMTP login failed' "$temp_root/verify-mail.out" || fail 'G9 SMTP login failure was not FAIL'
printf 'PASS: a failing G9 IMAP/SMTP login is FAIL\n'

dry="$temp_root/dry"
mkdir -p "$dry"
HOME="$dry" PATH="$safe:$PATH" bash "$install" --dry-run >"$temp_root/dry.out" 2>"$temp_root/dry.err" || fail 'dry-run'
HOME="$dry" PATH="$safe:$PATH" OAOS_BASE_DOMAIN=example.com bash "$install" --stage stack,ingress,mail --dry-run >"$temp_root/selected.out" 2>"$temp_root/selected.err" || fail 'selected dry-run'
[[ ! -e $dry/.oaos && ! -e $dry/.oaos-install && ! -e $dry/oaos && ! -e $dry/.hermes ]] || fail 'dry-run wrote to HOME'
for stage in stack ingress mail; do grep -Fq "Would run $stage" "$temp_root/selected.out" || fail "missing plan for $stage"; done
printf 'PASS: full and selected dry-run leave HOME untouched\n'
corrupt="$temp_root/corrupt"
mkdir -p "$corrupt/.oaos-install"
printf 'not JSON {\n' > "$corrupt/.oaos-install/state.json"
if HOME="$corrupt" PATH="$safe:$PATH" bash "$install" --status >"$temp_root/status.out" 2>"$temp_root/status.err"; then fail 'corrupt status succeeded'; fi
HOME="$corrupt" PATH="$safe:$PATH" bash "$install" --stage harness >"$temp_root/recover.out" 2>"$temp_root/recover.err" || fail 'corrupt recovery'
preserved=("$corrupt"/.oaos-install/state.json.corrupt-*)
[[ ${#preserved[@]} == 1 && -f ${preserved[0]} ]] || fail 'corrupt state not preserved'
grep -Fq 'not JSON {' "${preserved[0]}" || fail 'corrupt backup changed'
printf 'PASS: corrupt state preserved and recovered\n'
python3 - "$first/.oaos" "$first/.oaos-install" "$team_home/.oaos" "$team_home/.oaos-install" <<'PY' || fail 'secret-like values in logs/state'
import pathlib,re,sys
p=re.compile(rb'''(?:sk-|ghp_|nous_|AIza|xoxb-|AKIA|vck_)[A-Za-z0-9_-]+|[0-9]{8,10}:[A-Za-z0-9_-]{35,}|Bearer[ \t]+[A-Za-z0-9._~+/=-]+|(?:api[_-]?key|token|secret|password)["']?[ \t]*[:=][ \t]*["']?(?!\[REDACTED\])[^\s"']+|mmbottoken|ol_api_''',re.I)
for root in map(pathlib.Path,sys.argv[1:]):
 for f in root.rglob('*'):
  if f.is_file(): assert not p.search(f.read_bytes()),f
PY
printf 'PASS: log and state secret scan\n'

# --- static checks --------------------------------------------------------------------------
project_scripts=("$repo_root"/editions/project/*.sh "$repo_root/bootstrap/verify/project-verify.sh")
python3 - "$repo_root/editions/project" "$verify" <<'PY' || fail 'native service or ingress static validation'
import pathlib,sys
root=pathlib.Path(sys.argv[1]); verify=pathlib.Path(sys.argv[2]).read_text()
native=(root/'native-stack.sh').read_text()
unit=(root/'systemd/oaos-outline.service').read_text()
install=(root/'install.sh').read_text()
for term in ('native_postgres', 'native_redis', 'native_mattermost', 'native_outline', 'CREATE DATABASE mattermost', 'CREATE DATABASE outline',
             'bind 127.0.0.1', 'v1.10.1', 'patch-outline-bind.py', 'build/server/main.js', "listen_addresses = 'localhost'",
             'OAOS_OUTLINE_COMMIT=4a5a616a21be800257dc11cef4263d0dd0412156', 'git -C "$path" rev-parse HEAD', '[[ -x /usr/bin/yarn ]]',
             'render-outline-env.py'):
 assert term in native,term
for term in ('User=outline', 'EnvironmentFile=/etc/oaos/outline.env', 'ExecStart=/usr/bin/node /opt/outline/build/server/index.js',
             'ExecStartPre=/usr/bin/yarn db:migrate', 'Restart=on-failure'):
 assert term in unit,term
assert 'native_outline_config' in install and '"$mmctl" --local' in install
assert "'Native services'" in verify and "Loopback :$port" in verify
chat=(root/'ingress/chat.conf').read_text()
note=(root/'ingress/note.conf').read_text()
assert '127.0.0.1:8065' in chat and '127.0.0.1:3000' in note
assert 'client_max_body_size 50M;' in chat and 'client_max_body_size 50M;' in note
assert 'location ~ /api/v[0-9]+/(users/)?websocket$' in chat
assert 'Upgrade $http_upgrade' in chat and 'proxy_read_timeout 600s;' in chat
PY
printf 'PASS: native service and ingress static validation (full Outline SHA, /usr/bin node+yarn, PostgreSQL loopback)\n'
# §5.2: native systemd only; Project code never installs or calls Docker.
if grep -niE '\bdocker\b|docker-compose|compose up' "${project_scripts[@]}" "$repo_root"/editions/project/*.py >"$temp_root/docker.out"; then
  cat "$temp_root/docker.out" >&2; fail 'Project code references Docker'
fi
printf 'PASS: Project code has no Docker usage\n'
# Secrets never reach argv: no Bearer header on a command line, no secret via hermes config set.
# shellcheck disable=SC2016 # literal patterns
if grep -nE -- '-H[[:space:]]+"?Authorization|hermes config set[[:space:]]+(MATTERMOST_TOKEN|OUTLINE_API_TOKEN|OAOS_MAIL_PASSWORD|TELEGRAM_BOT_TOKEN|[A-Z_]*_API_KEY)' \
  "${project_scripts[@]}" >"$temp_root/argv-secret.out"; then
  cat "$temp_root/argv-secret.out" >&2; fail 'a Project script puts a secret on a command line'
fi
printf 'PASS: Project scripts keep tokens off argv\n'
patch_repo="$temp_root/patch-repo"
mkdir -p "$patch_repo/server"
printf 'export function start() {\n  server.listen(normalizedPort);\n}\n' > "$patch_repo/server/main.ts"
git -C "$patch_repo" init -q -b main
git -C "$patch_repo" add server/main.ts
git -C "$patch_repo" -c user.name='OAOS Test' -c user.email='test@localhost' commit -qm 'seed'
python3 "$repo_root/editions/project/patch-outline-bind.py" "$patch_repo" >"$temp_root/patch.out" || fail 'Outline bind patch'
python3 "$repo_root/editions/project/patch-outline-bind.py" "$patch_repo" >"$temp_root/patch-rerun.out" || fail 'Outline bind patch rerun'
[[ $(git -C "$patch_repo" diff --numstat -- server/main.ts) == $'1\t1\tserver/main.ts' ]] || fail 'Outline patch changed more than one line'
grep -Fq 'server.listen(normalizedPort, "127.0.0.1");' "$patch_repo/server/main.ts" || fail 'Outline patch missed loopback'
printf 'export function start() {\n  server.listen(normalizedPort, "0.0.0.0");\n}\n' > "$patch_repo/server/main.ts"
if python3 "$repo_root/editions/project/patch-outline-bind.py" "$patch_repo" >"$temp_root/patch-bad.out" 2>"$temp_root/patch-bad.err"; then fail 'Outline patch accepted unexpected source'; fi
grep -Fq 'server.listen(normalizedPort, "0.0.0.0");' "$patch_repo/server/main.ts" || fail 'Outline patch changed unexpected source'
printf 'PASS: pinned Outline bind patch is one line, repeatable, and rejects drift\n'

# Service accounts cannot traverse a 0750 home directory, so no service user
# may be handed a path from the repository or the invoking home directory.
# shellcheck disable=SC2016  # the literal $repo_root/$HOME text is the pattern being searched for
grep -nE 'runuser -u [a-z-]+ --[^|]*\$(repo_root|HOME)\b' "$repo_root"/editions/project/*.sh >"$temp_root/service-user-paths.out" 2>/dev/null && fail 'service user invoked with a repository or home path'
printf 'PASS: service users never receive repository or home paths\n'

# Under pipefail, "| grep -q" reports failure whenever grep exits before the producer is
# done. Capture the output first, then search it.
if grep -nE '\|[[:space:]]*grep -[a-zA-Z]*q' "$repo_root"/bootstrap/verify/*.sh "${project_scripts[@]}" >"$temp_root/pipefail-grep.out" 2>/dev/null; then
  cat "$temp_root/pipefail-grep.out" >&2
  fail 'verify and Project scripts must capture output before grepping'
fi
printf 'PASS: verifiers and Project scripts never pipe into grep -q under pipefail\n'
