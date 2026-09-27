#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
for name in ${!OAOS_@}; do unset "$name"; done
unset HERMES_HOME MSYSTEM LOCALAPPDATA APPDATA
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

first="$temp_root/first"
mkdir -p "$first"
HOME="$first" PATH="$mirror" bash "$install" --stage wiki,harness >"$temp_root/first.out" 2>"$temp_root/first.err" || fail 'first no-jq install failed'
[[ -f $first/.oaos-install/state.json ]] || fail 'first no-jq state file absent'
python3 - "$first/.oaos-install/state.json" <<'PY' || fail 'first no-jq state schema invalid'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    state = json.load(f)
assert state['edition'] == 'personal'
assert set(state['stages']) == {'prep', 'hermes', 'llm', 'telegram', 'gateway', 'wiki', 'harness', 'cron', 'verify'}
assert state['stages']['wiki']['status'] == 'done'
assert state['stages']['harness']['status'] == 'done'
PY
printf 'PASS: no-jq first write created nine-stage state\n'

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
assert len(state['stages']) == 9
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
[[ $1 == config && $2 == get ]] || exit 1
case $3:$4 in
  --raw:model) printf 'configured-model\n' ;;
  model:)
    printf 'model: configured-model\r\n'
    case $MOCK_LLM_CASE in configured|provider_only) printf 'provider: opencode-go\r\n' ;; esac ;;
  --raw:OPENCODE_GO_API_KEY)
    [[ $MOCK_LLM_CASE == configured ]] && printf '%s\n' "$MOCK_LLM_SECRET" ;;
  --raw:OPENAI_API_KEY)
    [[ $MOCK_LLM_CASE == legacy_key ]] && printf '%s\n' "$MOCK_LLM_SECRET" ;;
esac
SH
chmod 700 "$llm_bin/hermes"
for llm_case in configured no_provider provider_only legacy_key; do
  llm_home="$temp_root/llm-$llm_case"
  mkdir -p "$llm_home"
  code=0
  HOME="$llm_home" PATH="$llm_bin:$mirror" MOCK_LLM_CASE="$llm_case" MOCK_LLM_SECRET='sk-PRIVATE_TEST_KEY' \
    bash "$install" --stage llm > "$temp_root/llm-$llm_case.out" 2> "$temp_root/llm-$llm_case.err" || code=$?
  python3 - "$llm_home/.oaos-install/state.json" "$llm_case" "$code" <<'PY' || fail "LLM gate $llm_case"
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    status = json.load(f)['stages']['llm']['status']
expected = 'done' if sys.argv[2] in ('configured', 'legacy_key') else 'blocked'
assert status == expected, (status, expected)
assert int(sys.argv[3]) == (0 if expected == 'done' else 3)
PY
  if grep -FRq 'sk-PRIVATE_TEST_KEY' "$llm_home" "$temp_root/llm-$llm_case.out" "$temp_root/llm-$llm_case.err"; then
    fail "LLM key value leaked in $llm_case"
  fi
done
printf 'PASS: provider-derived and legacy LLM keys pass; missing keys block without leaking values\n'

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
HOME="$cron_home" PATH="$cron_bin:$mirror" MOCK_CRON_JOBS="$cron_jobs" MOCK_CRON_ARGS="$cron_args" \
  bash "$install" --stage cron > "$temp_root/linux-cron.out" 2> "$temp_root/linux-cron.err" || fail 'Linux cron registration'
[[ $(wc -l < "$cron_args") == 2 ]] || fail 'Linux cron registration count'
if ! { grep -Fxq 'oaos-daily-backup.sh' "$cron_args" && grep -Fxq 'oaos-gateway-watchdog.sh' "$cron_args"; }; then
  fail 'Linux cron used a path instead of a filename'
fi
[[ -x $cron_home/.hermes/scripts/oaos-daily-backup.sh && -x $cron_home/.hermes/scripts/oaos-gateway-watchdog.sh ]] || fail 'Linux cron scripts absent'

: > "$cron_jobs"
HOME="$cron_home" PATH="$cron_bin:$mirror" MOCK_CRON_JOBS="$cron_jobs" MOCK_CRON_ARGS="$cron_args" OAOS_STATE_FILE="$temp_root/linux-cron-identical-state.json" \
  bash "$install" --stage cron > "$temp_root/linux-cron-identical.out" 2> "$temp_root/linux-cron-identical.err" || fail 'identical Linux cron scripts blocked rerun'
[[ $(wc -l < "$cron_args") == 4 ]] || fail 'identical Linux cron scripts were not registered'

printf 'user-edited backup\n' > "$cron_home/.hermes/scripts/oaos-daily-backup.sh"
: > "$cron_jobs"
code=0
HOME="$cron_home" PATH="$cron_bin:$mirror" MOCK_CRON_JOBS="$cron_jobs" MOCK_CRON_ARGS="$cron_args" OAOS_STATE_FILE="$temp_root/linux-cron-different-state.json" \
  bash "$install" --stage cron > "$temp_root/linux-cron-different.out" 2> "$temp_root/linux-cron-different.err" || code=$?
[[ $code == 3 && $(cat "$cron_home/.hermes/scripts/oaos-daily-backup.sh") == 'user-edited backup' ]] || fail 'changed Linux cron script was not preserved'
[[ -z $(find "$cron_home/.hermes/scripts" -name '*.tmp.*' -print -quit) ]] || fail 'Linux cron left a temporary script'

retry_home="$temp_root/cron-retry-home"
mkdir -p "$retry_home"
retry_jobs="$temp_root/linux-cron-retry.jobs"
retry_args="$temp_root/linux-cron-retry.args"
code=0
HOME="$retry_home" PATH="$cron_bin:$mirror" MOCK_CRON_JOBS="$retry_jobs" MOCK_CRON_ARGS="$retry_args" MOCK_CRON_FAIL_ONCE=1 MOCK_CRON_FAIL_MARKER="$temp_root/cron-first-failure" \
  bash "$install" --stage cron > "$temp_root/linux-cron-first-failure.out" 2> "$temp_root/linux-cron-first-failure.err" || code=$?
[[ $code == 1 && -x $retry_home/.hermes/scripts/oaos-daily-backup.sh ]] || fail 'first failed cron registration did not leave a complete script'
HOME="$retry_home" PATH="$cron_bin:$mirror" MOCK_CRON_JOBS="$retry_jobs" MOCK_CRON_ARGS="$retry_args" \
  bash "$install" --stage cron > "$temp_root/linux-cron-retry.out" 2> "$temp_root/linux-cron-retry.err" || fail 'cron did not recover after initial registration failure'
[[ $(wc -l < "$retry_args") == 2 ]] || fail 'cron retry did not register both jobs'
printf 'PASS: Linux cron uses filenames, accepts identical scripts, preserves changed scripts, and retries after registration failure\n'
