#!/usr/bin/env bash
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
install="$repo_root/editions/project/install.sh"
fail() {
  printf 'FAIL: %s\n' "$1" >&2
  for file in "$temp_root"/*.out "$temp_root"/*.err; do [[ -f $file ]] && cat "$file" >&2; done
  exit 1
}
mirror="$temp_root/no-jq-bin"
mkdir -p "$mirror"
for command_path in /usr/bin/*; do
  [[ -f $command_path || -L $command_path ]] || continue
  name=${command_path##*/}
  [[ $name == jq ]] || ln -s -- "$command_path" "$mirror/$name"
done
[[ ! -e $mirror/jq && -x $mirror/python3 ]] || fail 'no-jq mirror invalid'
first="$temp_root/first"
mkdir -p "$first"
HOME="$first" PATH="$mirror" bash "$install" --stage wiki,harness >"$temp_root/first.out" 2>"$temp_root/first.err" || fail 'first install'
python3 - "$first/.oaos-install/state.json" <<'PY' || fail 'state schema'
import json,sys
x=json.load(open(sys.argv[1], encoding='utf-8'))
assert x['edition']=='project'
assert list(x['stages'])==['prep','hermes','llm','telegram','gateway','wiki','harness','cron','stack','ingress','mail','team','verify']
assert x['stages']['wiki']['status']==x['stages']['harness']['status']=='done'
PY
HOME="$first" PATH="$mirror" bash "$install" --stage wiki,harness >"$temp_root/second.out" 2>"$temp_root/second.err" || fail 'rerun'
grep -Fq 'Skipping wiki (done).' "$temp_root/second.out" || fail 'wiki not skipped'
grep -Fq 'Skipping harness (done).' "$temp_root/second.out" || fail 'harness not skipped'
printf 'PASS: no-jq state and rerun\n'
dry="$temp_root/dry"
mkdir -p "$dry"
HOME="$dry" bash "$install" --dry-run >"$temp_root/dry.out" 2>"$temp_root/dry.err" || fail 'dry-run'
HOME="$dry" bash "$install" --stage stack,ingress,mail --dry-run >"$temp_root/selected.out" 2>"$temp_root/selected.err" || fail 'selected dry-run'
[[ ! -e $dry/.oaos && ! -e $dry/.oaos-install && ! -e $dry/oaos ]] || fail 'dry-run wrote to HOME'
for stage in stack ingress mail; do grep -Fq "Would run $stage" "$temp_root/selected.out" || fail "missing plan for $stage"; done
printf 'PASS: full and selected dry-run leave HOME untouched\n'
corrupt="$temp_root/corrupt"
mkdir -p "$corrupt/.oaos-install"
printf 'not JSON {\n' > "$corrupt/.oaos-install/state.json"
if HOME="$corrupt" bash "$install" --status >"$temp_root/status.out" 2>"$temp_root/status.err"; then fail 'corrupt status succeeded'; fi
HOME="$corrupt" bash "$install" --stage harness >"$temp_root/recover.out" 2>"$temp_root/recover.err" || fail 'corrupt recovery'
preserved=("$corrupt"/.oaos-install/state.json.corrupt-*)
[[ ${#preserved[@]} == 1 && -f ${preserved[0]} ]] || fail 'corrupt state not preserved'
grep -Fq 'not JSON {' "${preserved[0]}" || fail 'corrupt backup changed'
printf 'PASS: corrupt state preserved and recovered\n'
python3 - "$first/.oaos" "$first/.oaos-install" <<'PY' || fail 'secret-like values in logs/state'
import pathlib,re,sys
p=re.compile(rb'(?:sk-|ghp_|nous_|AIza|xoxb-|AKIA|vck_)[A-Za-z0-9_-]+|[0-9]{8,10}:[A-Za-z0-9_-]{35,}|Bearer[ \t]+[A-Za-z0-9._~+/=-]+',re.I)
for root in map(pathlib.Path,sys.argv[1:]):
 for f in root.rglob('*'):
  if f.is_file(): assert not p.search(f.read_bytes()),f
PY
printf 'PASS: log and state secret scan\n'
python3 - "$repo_root/editions/project" <<'PY' || fail 'compose or ingress static validation'
import pathlib,sys,yaml
root=pathlib.Path(sys.argv[1]); data=yaml.safe_load((root/'compose.yaml').read_text())
assert set(data['services'])=={'postgres','redis','mattermost','outline'}
for name in ('mattermost','outline'):
 assert all(str(p).startswith('127.0.0.1:') for p in data['services'][name]['ports'])
for name in ('postgres','redis','mattermost','outline'):
 assert data['services'][name]['restart']=='unless-stopped'
 assert 'healthcheck' in data['services'][name]
assert data['services']['mattermost']['image'].endswith(':11.10.2')
assert data['services']['outline']['image'].endswith(':1.10.1')
assert 'ports' not in data['services']['postgres'] and 'ports' not in data['services']['redis']
assert 'mattermost' in (root/'init-databases.sh').read_text() and 'outline' in (root/'init-databases.sh').read_text()
assert '127.0.0.1:8065' in (root/'ingress/chat.conf').read_text()
assert '127.0.0.1:3000' in (root/'ingress/note.conf').read_text()
assert 'Upgrade $http_upgrade' in (root/'ingress/chat.conf').read_text()
PY
printf 'PASS: static Compose and ingress validation\n'
