#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
for name in ${!OAOS_@}; do unset "$name"; done
unset HERMES_HOME MSYSTEM LOCALAPPDATA APPDATA
# Forks run the same smoke; the owner check itself is exercised explicitly below.
export OAOS_ALLOW_FORK=1
install="$repo_root/editions/personal/install.sh"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  for file in "$temp_root"/*.out "$temp_root"/*.err; do
    [[ -f $file ]] && { printf '\n--- %s ---\n' "$file" >&2; cat "$file" >&2; }
  done
  exit 1
}

# A PATH mirror hides jq while preserving ordinary system utilities.
mirror="$temp_root/no-jq-bin"
mkdir -p "$mirror"
for command_path in /usr/bin/*; do
  [[ -f $command_path || -L $command_path ]] || continue
  name=${command_path##*/}
  [[ $name == jq ]] || ln -s -- "$command_path" "$mirror/$name"
done
[[ ! -e $mirror/jq ]] || fail 'jq is visible in the no-jq PATH'
[[ -x $mirror/python3 ]] || fail 'python3 is unavailable in the no-jq PATH'
# Host service managers are always mocked: no test may reach the real user manager.
for name in systemctl loginctl ss; do rm -f -- "$mirror/$name"; done
cat > "$mirror/systemctl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${MOCK_SYSTEMCTL_LOG:-/dev/null}"
case $* in *is-enabled*|*is-active*) [[ -n ${MOCK_SYSTEMCTL_LOG:-} ]] ;; esac
SH
printf '#!/usr/bin/env bash\nexit 0\n' > "$mirror/ss"
chmod 700 "$mirror/systemctl" "$mirror/ss"

first="$temp_root/first"
mkdir -p "$first"
HOME="$first" PATH="$mirror" bash "$install" --stage wiki,harness >"$temp_root/first.out" 2>"$temp_root/first.err" || fail 'first no-jq install failed'
[[ -f $first/.oaos-install/state.json ]] || fail 'first no-jq state file absent'
python3 - "$first/.oaos-install/state.json" <<'PY' || fail 'first no-jq state schema invalid'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    state = json.load(f)
assert state['edition'] == 'personal'
assert set(state['stages']) == {'prep', 'hermes', 'llm', 'telegram', 'gateway', 'wiki', 'harness', 'cron', 'mail', 'verify'}
assert state['stages']['wiki']['status'] == 'done'
assert state['stages']['harness']['status'] == 'done'
assert set(state['editions']) == {'personal'}
assert state['editions']['personal']['stages']['wiki']['status'] == 'done'
PY
[[ -x $first/data/wiki/.git/hooks/pre-commit ]] || fail 'wiki pre-commit secret hook absent'
[[ $(git --git-dir="$first/data/wiki.git" rev-parse --is-bare-repository) == true ]] || fail 'wiki relay is not a bare repository'
[[ $(stat -c %a "$first/data/wiki.git") == 700 ]] || fail 'wiki relay is not 700'
[[ $(git -C "$first/data/wiki" remote get-url origin) == "$first/data/wiki.git" ]] || fail 'wiki origin is not the relay'
[[ $(git --git-dir="$first/data/wiki.git" rev-parse main) == $(git -C "$first/data/wiki" rev-parse HEAD) ]] || fail 'relay does not hold the seed commit'
grep -Fq 'Host runs Linux' "$first/.hermes/memories/MEMORY.md" || fail 'MEMORY host facts not filled'
! grep -Fq '{{host' "$first/.hermes/memories/MEMORY.md" || fail 'MEMORY host placeholder left'
printf 'sk-THISISATESTSECRETVALUE\n' > "$first/data/wiki/leak.md"
git -C "$first/data/wiki" add leak.md
if git -C "$first/data/wiki" -c user.name=T -c user.email=t@localhost commit -qm leak >/dev/null 2>&1; then fail 'wiki hook accepted a secret'; fi
git -C "$first/data/wiki" rm -q --cached leak.md && rm -f "$first/data/wiki/leak.md"
printf 'PASS: no-jq first write created ten-stage edition-scoped state, wiki secret hook, pushed bare relay (700), and MEMORY host facts\n'

# An existing wiki whose origin is a remote of the owner keeps it; the relay is added as oaos-relay.
own_remote="$temp_root/own-remote"
mkdir -p "$own_remote/data"
git init -q -b main "$own_remote/data/wiki"
git -C "$own_remote/data/wiki" -c user.name=T -c user.email=t@localhost commit -q --allow-empty -m own
git -C "$own_remote/data/wiki" remote add origin 'ssh://git@example.com/me/wiki.git'
HOME="$own_remote" PATH="$mirror" bash "$install" --stage wiki >"$temp_root/own-remote.out" 2>"$temp_root/own-remote.err" || fail 'existing wiki with its own origin'
[[ $(git -C "$own_remote/data/wiki" remote get-url origin) == 'ssh://git@example.com/me/wiki.git' ]] || fail 'owner origin was replaced'
[[ $(git -C "$own_remote/data/wiki" remote get-url oaos-relay) == "$own_remote/data/wiki.git" ]] || fail 'relay not added as oaos-relay'
[[ $(git --git-dir="$own_remote/data/wiki.git" rev-parse main) == $(git -C "$own_remote/data/wiki" rev-parse HEAD) ]] || fail 'existing wiki not pushed to the relay'
mkdir -p "$temp_root/not-bare/data/wiki.git"
git init -q -b main "$temp_root/not-bare/data/wiki"
git -C "$temp_root/not-bare/data/wiki" -c user.name=T -c user.email=t@localhost commit -q --allow-empty -m own
code=0
HOME="$temp_root/not-bare" PATH="$mirror" bash "$install" --stage wiki >"$temp_root/not-bare.out" 2>"$temp_root/not-bare.err" || code=$?
[[ $code == 3 ]] && grep -Fq 'is not a bare git repository; preserving it' "$temp_root/not-bare.err" || fail 'non-bare relay path was not preserved and blocked'
seed_head=$(git -C "$first/data/wiki" rev-parse HEAD)
restore="$temp_root/restore"
mkdir -p "$restore/data"
cp -a "$first/data/wiki.git" "$restore/data/wiki.git"
HOME="$restore" PATH="$mirror" bash "$install" --stage wiki >"$temp_root/restore.out" 2>"$temp_root/restore.err" || fail 'wiki restore from relay'
[[ $(git -C "$restore/data/wiki" rev-parse HEAD) == "$seed_head" ]] || fail 'missing wiki was reseeded instead of restored from the relay'
grep -Fq 'Restored the wiki from its relay' "$temp_root/restore.out" || fail 'relay restore not reported'
[[ -x $restore/data/wiki/.git/hooks/pre-commit ]] || fail 'restored wiki lacks the secret hook'
printf 'PASS: an existing origin is kept (relay as oaos-relay); a missing wiki is restored from the relay; a non-bare relay path blocks without changes\n'

HOME="$first" PATH="$mirror" bash "$install" --stage wiki,harness >"$temp_root/second.out" 2>"$temp_root/second.err" || fail 'second no-jq install failed'
grep -Fq 'Skipping wiki (done).' "$temp_root/second.out" || fail 'wiki did not skip on rerun'
grep -Fq 'Skipping harness (done).' "$temp_root/second.out" || fail 'harness did not skip on rerun'
printf 'PASS: rerun skipped completed wiki and harness\n'

dry="$temp_root/dry"
mkdir -p "$dry"
HOME="$dry" bash "$install" --dry-run >"$temp_root/dry.out" 2>"$temp_root/dry.err" || fail 'dry-run failed'
[[ ! -e $dry/.oaos/logs && ! -e $dry/.oaos-install/state.json ]] || fail 'dry-run wrote a log or state file'
printf 'PASS: dry-run wrote no log or state file\n'

corrupt="$temp_root/corrupt"
mkdir -p "$corrupt/.oaos-install"
printf 'not JSON {\n' > "$corrupt/.oaos-install/state.json"
if HOME="$corrupt" bash "$install" --status >"$temp_root/status.out" 2>"$temp_root/status.err"; then fail 'corrupt --status returned success'; fi
grep -Fq 'invalid JSON' "$temp_root/status.err" || fail 'corrupt --status omitted its error'
[[ ! -e $corrupt/.oaos/logs ]] || fail 'corrupt --status wrote a log'
HOME="$corrupt" bash "$install" --stage harness >"$temp_root/recover.out" 2>"$temp_root/recover.err" || fail 'corrupt state recovery failed'
preserved=("$corrupt"/.oaos-install/state.json.corrupt-*)
[[ ${#preserved[@]} == 1 && -f ${preserved[0]} ]] || fail 'corrupt state was not preserved'
grep -Fq 'not JSON {' "${preserved[0]}" || fail 'preserved corrupt state changed'
python3 - "$corrupt/.oaos-install/state.json" <<'PY' || fail 'new state after recovery is invalid'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    state = json.load(f)
assert len(state['stages']) == 10
assert state['stages']['harness']['status'] == 'done'
PY
printf 'PASS: corrupt state reported, preserved, and replaced on resume\n'

# A normal install must leave no raw secret-like values in logs or state.
python3 - "$first/.oaos" "$first/.oaos-install" <<'PY' || fail 'normal install has a secret-like log or state value'
import pathlib, re, sys
pattern = re.compile(rb'(?:sk-|ghp_|nous_|AIza|xoxb-|AKIA|vck_)[A-Za-z0-9_-]+|[0-9]{8,10}:[A-Za-z0-9_-]{35,}|Bearer[ \t]+[A-Za-z0-9._~+/=-]+', re.I)
for root in map(pathlib.Path, sys.argv[1:]):
    for path in root.rglob('*'):
        if path.is_file():
            assert not pattern.search(path.read_bytes()), path
PY
printf 'PASS: normal logs and state contain zero raw secret-like matches\n'

llm_bin="$temp_root/llm-bin"
mkdir -p "$llm_bin"
cat > "$llm_bin/hermes" <<'SH'
#!/usr/bin/env bash
case $1:${2:-} in
  config:get)
    case $3:${4:-} in
      --raw:model) [[ $MOCK_LLM_CASE == no_model ]] || printf 'configured-model\n' ;;
      --raw:OPENCODE_GO_API_KEY) printf '%s\n' "$MOCK_LLM_SECRET" ;;
    esac ;;
  chat:-q) printf 'chat\n' >> "$MOCK_LLM_CHAT"; [[ $MOCK_LLM_CASE == probe_ok ]] && printf 'OK\n' ;;
  *) exit 1 ;;
esac
SH
chmod 700 "$llm_bin/hermes"
for llm_case in probe_ok probe_fail no_model; do
  llm_home="$temp_root/llm-$llm_case"
  mkdir -p "$llm_home"
  code=0
  HOME="$llm_home" PATH="$llm_bin:$mirror" MOCK_LLM_CASE="$llm_case" MOCK_LLM_SECRET='sk-PRIVATE_TEST_KEY' MOCK_LLM_CHAT="$temp_root/llm-$llm_case.chat" \
    bash "$install" --stage llm > "$temp_root/llm-$llm_case.out" 2> "$temp_root/llm-$llm_case.err" || code=$?
  python3 - "$llm_home/.oaos-install/state.json" "$llm_case" "$code" <<'PY' || fail "LLM gate $llm_case"
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    status = json.load(f)['editions']['personal']['stages']['llm']['status']
expected = 'done' if sys.argv[2] == 'probe_ok' else 'blocked'
assert status == expected, (status, expected)
assert int(sys.argv[3]) == (0 if expected == 'done' else 3)
PY
  if [[ $llm_case == no_model ]]; then [[ ! -e $temp_root/llm-$llm_case.chat ]] || fail 'probe sent without a model'
  else [[ -s $temp_root/llm-$llm_case.chat ]] || fail "G1 probe not sent ($llm_case)"; fi
  grep -Fq 'oaos-secret-set' "$temp_root/llm-$llm_case.err" || [[ $llm_case == probe_ok ]] || fail 'G1 hint lacks the secret helper'
  if grep -FRq 'sk-PRIVATE_TEST_KEY' "$llm_home" "$temp_root/llm-$llm_case.out" "$temp_root/llm-$llm_case.err"; then
    fail "LLM key value leaked in $llm_case"
  fi
done
printf 'PASS: G1 passes only on a working model probe (OAuth or key) and blocks with the secret helper hint\n'

tg_bin="$temp_root/tg-bin"
mkdir -p "$tg_bin"
cat > "$tg_bin/hermes" <<'SH'
#!/usr/bin/env bash
case $1:${2:-} in
  config:get)
    case ${4:-} in
      TELEGRAM_BOT_TOKEN) printf '%s\n' "$MOCK_TG_TOKEN" ;;
      TELEGRAM_ALLOWED_USERS) printf '%s\n' "$MOCK_TG_USERS" ;;
      approvals.*) [[ -f $MOCK_APPROVALS ]] && grep -F "${4}=" "$MOCK_APPROVALS" | tail -n 1 | cut -d= -f2 ;;
    esac ;;
  config:set) printf '%s=%s\n' "$3" "$4" >> "$MOCK_APPROVALS" ;;
  gateway:status) [[ -f $MOCK_GW_RUNNING ]] && printf 'running\n' ;;
  gateway:restart) printf 'restart\n' >> "$MOCK_GW_LOG" ;;
  gateway:install) : > "$MOCK_GW_RUNNING"; printf 'install\n' >> "$MOCK_GW_LOG" ;;
  *) exit 1 ;;
esac
SH
cat > "$tg_bin/curl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MOCK_CURL_ARGV"
config=$(cat)
case $config in *"$MOCK_GOOD_TOKEN"*) printf '{"ok":true,"result":{"is_bot":true}}' ;; *) printf '{"ok":false,"error_code":401}' ;; esac
SH
cat > "$tg_bin/loginctl" <<'SH'
#!/usr/bin/env bash
printf 'yes\n'
SH
chmod 700 "$tg_bin/"*
tg_home="$temp_root/tg-home"
mkdir -p "$tg_home/.config/systemd/user/default.target.wants"
printf '[Service]\n' > "$tg_home/.config/systemd/user/hermes-gateway.service"
tg_env=(HOME="$tg_home" PATH="$tg_bin:$mirror" MOCK_GOOD_TOKEN='123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' MOCK_CURL_ARGV="$temp_root/curl.args"
  MOCK_APPROVALS="$temp_root/approvals" MOCK_GW_RUNNING="$temp_root/gw.running" MOCK_GW_LOG="$temp_root/gw.log" MOCK_SYSTEMCTL_LOG="$temp_root/tg-systemctl.log")
code=0
env "${tg_env[@]}" MOCK_TG_TOKEN='123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' MOCK_TG_USERS='@someone' \
  bash "$install" --stage telegram,gateway > "$temp_root/tg1.out" 2> "$temp_root/tg1.err" || code=$?
[[ $code == 3 ]] || fail 'non-numeric allowlist was accepted'
grep -Fq 'gateway blocked' "$temp_root/tg1.err" || fail 'gateway ran while telegram was blocked'
[[ ! -e $temp_root/gw.log ]] || fail 'gateway installed before telegram completed'
code=0
env "${tg_env[@]}" MOCK_TG_TOKEN='987654321:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB' MOCK_TG_USERS='12345' \
  bash "$install" --stage telegram > "$temp_root/tg2.out" 2> "$temp_root/tg2.err" || code=$?
[[ $code == 3 ]] && grep -Fq 'getMe failed (HTTP 401)' "$temp_root/tg2.err" || fail 'rejected bot token was accepted'
env "${tg_env[@]}" MOCK_TG_TOKEN='123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' MOCK_TG_USERS='12345, 67890' \
  bash "$install" --stage telegram,gateway > "$temp_root/tg3.out" 2> "$temp_root/tg3.err" || fail 'valid telegram + gateway'
grep -Fq 'install' "$temp_root/gw.log" || fail 'gateway was not installed'
for key in cron_mode single_query_mode unattended_mode; do
  grep -Fxq "approvals.$key=deny" "$temp_root/approvals" || fail "unattended-deny policy not set ($key)"
done
env "${tg_env[@]}" MOCK_TG_TOKEN='123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' MOCK_TG_USERS='12345' \
  bash "$install" --stage telegram --recheck > "$temp_root/tg4.out" 2> "$temp_root/tg4.err" || fail 'telegram recheck'
grep -Fxq 'restart' "$temp_root/gw.log" || fail 'running gateway was not restarted after telegram changed'
if grep -Eq '[0-9]{9}:[A-Z]{35}' "$temp_root/curl.args" "$temp_root"/tg*.out "$temp_root"/tg*.err; then fail 'bot token reached curl argv or output'; fi
if grep -FRq 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' "$tg_home/.oaos" "$tg_home/.oaos-install"; then fail 'bot token reached logs or state'; fi
printf 'PASS: G2 getMe without argv token, G3 numeric IDs, gateway waits for telegram, unattended-deny set, restart on change\n'

wiki_fail="$temp_root/wiki-fail"
mkdir -p "$wiki_fail"
printf 'not a directory\n' > "$wiki_fail/data"
code=0
HOME="$wiki_fail" PATH="$mirror" bash "$install" --stage wiki > "$temp_root/wiki-fail.out" 2> "$temp_root/wiki-fail.err" || code=$?
[[ $code == 1 ]] || fail 'wiki stage with an impossible path did not fail closed'
python3 - "$wiki_fail/.oaos-install/state.json" <<'PY' || fail 'failed wiki stage recorded as done'
import json, sys
assert json.load(open(sys.argv[1]))['editions']['personal']['stages']['wiki']['status'] == 'failed'
PY
printf 'PASS: an unguarded command failure fails the stage closed\n'

owner_home="$temp_root/owner"
mkdir -p "$owner_home"
code=0
HOME="$owner_home" PATH="$mirror" OAOS_ALLOW_FORK=0 OAOS_TEST_MODE=1 OAOS_TEST_ORIGIN='https://github.com/someone/open-agent-os.git' \
  bash "$install" --stage wiki > "$temp_root/owner.out" 2> "$temp_root/owner.err" || code=$?
[[ $code == 3 && ! -e $owner_home/data/wiki ]] || fail 'fork origin was not refused'
HOME="$owner_home" PATH="$mirror" OAOS_ALLOW_FORK=0 OAOS_TEST_MODE=1 OAOS_TEST_ORIGIN='git@github.com:openit-ai/open-agent-os.git' \
  bash "$install" --stage wiki > "$temp_root/owner2.out" 2> "$temp_root/owner2.err" || fail 'official origin refused'
HOME="$owner_home" PATH="$mirror" OAOS_ALLOW_FORK=1 OAOS_TEST_MODE=1 OAOS_TEST_ORIGIN='https://github.com/someone/open-agent-os.git' \
  bash "$install" --stage wiki > "$temp_root/owner3.out" 2> "$temp_root/owner3.err" || fail 'OAOS_ALLOW_FORK=1 did not allow a fork'
printf 'PASS: repository owner check refuses forks unless OAOS_ALLOW_FORK=1\n'

hermes_home_dir="$temp_root/hermes-stage"
mkdir -p "$hermes_home_dir/.oaos/cache" "$temp_root/hermes-bin"
cat > "$temp_root/hermes-bin/curl" <<'SH'
#!/usr/bin/env bash
while (($#)); do [[ $1 == -o ]] && { printf '# fresh installer\n' > "$2"; exit 0; }; shift; done
exit 1
SH
chmod 700 "$temp_root/hermes-bin/curl"
printf '# stale installer\n' > "$hermes_home_dir/.oaos/cache/hermes-install.sh"
touch -d '3 days ago' "$hermes_home_dir/.oaos/cache/hermes-install.sh"
code=0
HOME="$hermes_home_dir" PATH="$temp_root/hermes-bin:$mirror" bash "$install" --stage hermes < /dev/null > "$temp_root/hermes.out" 2> "$temp_root/hermes.err" || code=$?
[[ $code == 3 ]] || fail 'Hermes installer ran without approval'
grep -Fxq '# fresh installer' "$hermes_home_dir/.oaos/cache/hermes-install.sh" || fail 'stale cached installer was reused'
printf 'PASS: stale Hermes installer cache is downloaded again and still awaits approval\n'

verify_home="$temp_root/verify-home"
mkdir -p "$verify_home"
HOME="$verify_home" PATH="$mirror" bash "$install" --stage wiki > /dev/null 2>&1 || fail 'wiki before verify'
code=0
HOME="$verify_home" PATH="$mirror" bash "$install" --reverify > "$temp_root/verify1.out" 2> "$temp_root/verify1.err" || code=$?
HOME="$verify_home" PATH="$mirror" bash "$install" --stage verify > "$temp_root/verify2.out" 2> "$temp_root/verify2.err" || code=$?
grep -Fq 'Running verify.' "$temp_root/verify2.out" || fail 'verify was skipped on rerun'
[[ $code == 1 ]] || fail 'verify FAIL did not exit 1'
python3 - "$verify_home/.oaos-install/state.json" <<'PY' || fail 'verify FAIL recorded as success'
import json, sys
assert json.load(open(sys.argv[1]))['editions']['personal']['stages']['verify']['status'] == 'failed'
PY
printf 'PASS: verify always reruns and a FAIL is recorded as failed with exit 1\n'

cron_bin="$temp_root/cron-bin"
cron_home="$temp_root/cron-home"
mkdir -p "$cron_bin" "$cron_home"
cat > "$cron_bin/hermes" <<'SH'
#!/usr/bin/env bash
[[ $1 == cron ]] || exit 1
case $2 in
  list) [[ ! -f $MOCK_CRON_JOBS ]] || cat "$MOCK_CRON_JOBS" ;;
  create)
    name='' script=''
    shift 2
    while (($#)); do
      case $1 in --name) name=$2; shift ;; --script) script=$2; shift ;; esac
      shift
    done
    [[ $script == "$name.sh" ]] || exit 91
    if [[ ${MOCK_CRON_FAIL_ONCE:-0} == 1 && ! -e ${MOCK_CRON_FAIL_MARKER:-} ]]; then
      : > "$MOCK_CRON_FAIL_MARKER"
      exit 1
    fi
    printf '%s\n' "$script" >> "$MOCK_CRON_ARGS"
    printf '%s\n' "$name" >> "$MOCK_CRON_JOBS" ;;
  *) exit 1 ;;
esac
SH
chmod 700 "$cron_bin/hermes"
cron_jobs="$temp_root/linux-cron.jobs"
cron_args="$temp_root/linux-cron.args"
cron_env=(MOCK_CRON_JOBS="$cron_jobs" MOCK_CRON_ARGS="$cron_args" MOCK_SYSTEMCTL_LOG="$temp_root/cron-systemctl.log")
HOME="$cron_home" PATH="$cron_bin:$mirror" env "${cron_env[@]}" \
  bash "$install" --stage cron > "$temp_root/linux-cron.out" 2> "$temp_root/linux-cron.err" || fail 'Linux cron registration'
[[ $(cat "$cron_args") == 'oaos-daily-backup.sh' ]] || fail 'Linux cron must register only the backup script, by filename'
[[ -x $cron_home/.hermes/scripts/oaos-daily-backup.sh && -x $cron_home/.oaos/bin/oaos-gateway-watchdog.sh ]] || fail 'Linux backup or watchdog script absent'
[[ -f $cron_home/.config/systemd/user/oaos-gateway-watchdog.timer && -f $cron_home/.config/systemd/user/oaos-gateway-watchdog.service ]] || fail 'watchdog systemd units absent'
grep -Fq 'enable --now oaos-gateway-watchdog.timer' "$temp_root/cron-systemctl.log" || fail 'watchdog timer not enabled'
grep -Fq "$cron_home/.oaos/bin/oaos-gateway-watchdog.sh" "$cron_home/.config/systemd/user/oaos-gateway-watchdog.service" || fail 'watchdog unit path'
[[ $(stat -c %a "$cron_home/.oaos/backups") == 700 ]] || fail 'backup directory is not 700'
bash -n "$cron_home/.hermes/scripts/oaos-daily-backup.sh" "$cron_home/.oaos/bin/oaos-gateway-watchdog.sh" || fail 'generated script syntax'

# The generated backup honours HERMES_HOME and writes an owner-only archive.
custom_home="$temp_root/cron-custom"
mkdir -p "$custom_home" "$temp_root/custom-hermes"
HOME="$custom_home" HERMES_HOME="$temp_root/custom-hermes" PATH="$mirror" bash "$install" --stage wiki > /dev/null 2>&1 || fail 'wiki before custom cron'
printf 'y\n' > "$temp_root/custom-hermes/SOUL.md"
HOME="$custom_home" HERMES_HOME="$temp_root/custom-hermes" PATH="$cron_bin:$mirror" env MOCK_CRON_JOBS="$temp_root/custom.jobs" MOCK_CRON_ARGS="$temp_root/custom.args" MOCK_SYSTEMCTL_LOG="$temp_root/custom-systemctl.log" \
  bash "$install" --stage cron > "$temp_root/custom-cron.out" 2> "$temp_root/custom-cron.err" || fail 'cron with HERMES_HOME'
[[ -x $temp_root/custom-hermes/scripts/oaos-daily-backup.sh ]] || fail 'Linux scripts path ignored HERMES_HOME'
HOME="$custom_home" PATH="$mirror" bash "$temp_root/custom-hermes/scripts/oaos-daily-backup.sh" || fail 'generated Linux backup run'
archive=$(find "$custom_home/.oaos/backups" -name 'oaos-*.tar.gz' -print -quit)
[[ -n $archive && $(stat -c %a "$archive") == 600 ]] || fail 'backup archive absent or not 600'
archive_list=$(tar -tzf "$archive") || fail 'backup archive could not be listed'
grep -Fxq 'custom-hermes/SOUL.md' <<<"$archive_list" || fail 'backup did not include the custom HERMES_HOME'
printf 'x\n' > "$custom_home/data/wiki/note.md"
git -C "$custom_home/data/wiki" add note.md
git -C "$custom_home/data/wiki" -c user.name=T -c user.email=t@localhost commit -qm note
HOME="$custom_home" PATH="$mirror" bash "$temp_root/custom-hermes/scripts/oaos-daily-backup.sh" || fail 'backup run after a wiki commit'
[[ $(git --git-dir="$custom_home/data/wiki.git" rev-parse main) == $(git -C "$custom_home/data/wiki" rev-parse HEAD) ]] || fail 'daily backup did not push the wiki relay'
printf 'PASS: Linux backup honours HERMES_HOME with a 700 directory and 600 archive, and pushes the wiki relay\n'

# A backup script from the previous release (archive only, job registered) is upgraded in place.
backup_script="$temp_root/custom-hermes/scripts/oaos-daily-backup.sh"
sed '/^# OAOS wiki relay push/,$d' "$backup_script" > "$temp_root/legacy-backup.sh"
cp "$temp_root/legacy-backup.sh" "$backup_script"
HOME="$custom_home" HERMES_HOME="$temp_root/custom-hermes" PATH="$cron_bin:$mirror" env MOCK_CRON_JOBS="$temp_root/custom.jobs" MOCK_CRON_ARGS="$temp_root/custom.args" MOCK_SYSTEMCTL_LOG="$temp_root/custom-systemctl.log" \
  OAOS_STATE_FILE="$temp_root/custom-upgrade-state.json" bash "$install" --stage cron > "$temp_root/custom-upgrade.out" 2> "$temp_root/custom-upgrade.err" || fail 'cron upgrade of a legacy backup script'
grep -Fq '# OAOS wiki relay push' "$backup_script" || fail 'legacy backup script was not upgraded'
grep -Fq 'Upgrading the oaos-daily-backup script' "$temp_root/custom-upgrade.out" || fail 'backup upgrade not reported'
[[ $(wc -l < "$temp_root/custom.args") == 1 ]] || fail 'upgrade registered the backup job twice'
printf 'PASS: a previous-release backup script is upgraded in place to push the relay\n'

: > "$cron_jobs"
HOME="$cron_home" PATH="$cron_bin:$mirror" env "${cron_env[@]}" OAOS_STATE_FILE="$temp_root/linux-cron-identical-state.json" \
  bash "$install" --stage cron > "$temp_root/linux-cron-identical.out" 2> "$temp_root/linux-cron-identical.err" || fail 'identical Linux cron scripts blocked rerun'
[[ $(wc -l < "$cron_args") == 2 ]] || fail 'identical Linux cron scripts were not registered'

printf 'user-edited backup\n' > "$cron_home/.hermes/scripts/oaos-daily-backup.sh"
: > "$cron_jobs"
code=0
HOME="$cron_home" PATH="$cron_bin:$mirror" env "${cron_env[@]}" OAOS_STATE_FILE="$temp_root/linux-cron-different-state.json" \
  bash "$install" --stage cron > "$temp_root/linux-cron-different.out" 2> "$temp_root/linux-cron-different.err" || code=$?
[[ $code == 3 && $(cat "$cron_home/.hermes/scripts/oaos-daily-backup.sh") == 'user-edited backup' ]] || fail 'changed Linux cron script was not preserved'
[[ -z $(find "$cron_home/.hermes/scripts" "$cron_home/.oaos/bin" -name '*.tmp.*' -print -quit) ]] || fail 'Linux cron left a temporary script'

printf 'oaos-daily-backup\noaos-gateway-watchdog\n' > "$cron_jobs"
HOME="$cron_home" PATH="$cron_bin:$mirror" env "${cron_env[@]}" OAOS_STATE_FILE="$temp_root/linux-cron-legacy-state.json" \
  bash "$install" --stage cron > "$temp_root/linux-cron-legacy.out" 2> "$temp_root/linux-cron-legacy.err" || fail 'legacy watchdog job blocked cron'
grep -Fq 'legacy Hermes cron job oaos-gateway-watchdog' "$temp_root/linux-cron-legacy.err" || fail 'legacy watchdog job not reported'

retry_home="$temp_root/cron-retry-home"
mkdir -p "$retry_home"
retry_jobs="$temp_root/linux-cron-retry.jobs"
retry_args="$temp_root/linux-cron-retry.args"
code=0
HOME="$retry_home" PATH="$cron_bin:$mirror" env MOCK_CRON_JOBS="$retry_jobs" MOCK_CRON_ARGS="$retry_args" MOCK_CRON_FAIL_ONCE=1 MOCK_CRON_FAIL_MARKER="$temp_root/cron-first-failure" MOCK_SYSTEMCTL_LOG="$temp_root/retry-systemctl.log" \
  bash "$install" --stage cron > "$temp_root/linux-cron-first-failure.out" 2> "$temp_root/linux-cron-first-failure.err" || code=$?
[[ $code == 1 && -x $retry_home/.hermes/scripts/oaos-daily-backup.sh ]] || fail 'first failed cron registration did not leave a complete script'
HOME="$retry_home" PATH="$cron_bin:$mirror" env MOCK_CRON_JOBS="$retry_jobs" MOCK_CRON_ARGS="$retry_args" MOCK_SYSTEMCTL_LOG="$temp_root/retry-systemctl.log" \
  bash "$install" --stage cron > "$temp_root/linux-cron-retry.out" 2> "$temp_root/linux-cron-retry.err" || fail 'cron did not recover after initial registration failure'
[[ $(wc -l < "$retry_args") == 1 ]] || fail 'cron retry did not register the backup job'
printf 'PASS: Linux cron registers backup by filename, watchdog as a systemd user timer, preserves changed scripts, retries\n'

cu_bin="$temp_root/cu-bin"
cu_home="$temp_root/cu-home"
mkdir -p "$cu_bin" "$cu_home/.oaos-install"
cat > "$cu_bin/hermes" <<'SH'
#!/usr/bin/env bash
case $1:$2 in
  config:get) [[ -f $MOCK_CU ]] && cat "$MOCK_CU" ;;
  config:set) printf '%s\n' "$4" > "$MOCK_CU" ;;
  *) exit 1 ;;
esac
SH
chmod 700 "$cu_bin/hermes"
policy="$repo_root/editions/personal/computer-use-policy.sh"
cu_env=(HOME="$cu_home" PATH="$cu_bin:$mirror" MOCK_CU="$temp_root/cu.value")
printf '{"edition":"project","stages":{"prep":{"status":"done"}}}\n' > "$cu_home/.oaos-install/state.json"
code=0; printf 'YES\n' | env "${cu_env[@]}" bash "$policy" enable > /dev/null 2>&1 || code=$?
[[ $code == 3 && ! -e $temp_root/cu.value ]] || fail 'credential typing enabled on a Project host'
printf '{"edition":"personal","stages":{"prep":{"status":"done"}}}\n' > "$cu_home/.oaos-install/state.json"
code=0; printf 'YES\n' | env "${cu_env[@]}" SSH_CONNECTION='10.0.0.1 1 10.0.0.2 22' bash "$policy" enable > /dev/null 2>&1 || code=$?
[[ $code == 3 && ! -e $temp_root/cu.value ]] || fail 'credential typing enabled over SSH'
printf 'YES\n' | env -u SSH_CONNECTION -u SSH_CLIENT -u SSH_TTY "${cu_env[@]}" bash "$policy" enable > /dev/null 2>&1 || fail 'local Personal opt-in'
[[ $(cat "$temp_root/cu.value") == true && $(stat -c %a "$cu_home/.oaos/computer-use-consent") == 600 ]] || fail 'opt-in or consent protection'
printf 'PASS: credential-typing opt-in refuses SSH and non-Personal hosts; consent record is 600\n'
