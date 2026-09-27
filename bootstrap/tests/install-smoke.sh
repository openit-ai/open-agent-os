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
