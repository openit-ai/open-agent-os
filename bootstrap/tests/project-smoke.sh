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
assert list(x['stages'])==['prep','hermes','llm','telegram','wiki','harness','cron','stack','ingress','mail','team','gateway','verify']
assert x['stages']['wiki']['status']==x['stages']['harness']['status']=='done'
PY
HOME="$first" PATH="$mirror" bash "$install" --stage wiki,harness >"$temp_root/second.out" 2>"$temp_root/second.err" || fail 'rerun'
grep -Fq 'Skipping wiki (done).' "$temp_root/second.out" || fail 'wiki not skipped'
grep -Fq 'Skipping harness (done).' "$temp_root/second.out" || fail 'harness not skipped'
printf 'PASS: no-jq state and rerun\n'
cron_home="$temp_root/cron-home"
cron_bin="$temp_root/cron-bin"
mkdir -p "$cron_home" "$cron_bin"
touch "$temp_root/cron-list"
cat > "$cron_bin/hermes" <<'MOCK'
#!/usr/bin/env bash
set -Eeuo pipefail
case "$1 $2" in
  'cron list') cat "$OAOS_FAKE_CRON_LIST" ;;
  'cron create') printf 'oaos-daily-backup\n' >> "$OAOS_FAKE_CRON_LIST" ;;
  *) exit 1 ;;
esac
MOCK
chmod 700 "$cron_bin/hermes"
HOME="$cron_home" PATH="$cron_bin:$PATH" OAOS_FAKE_CRON_LIST="$temp_root/cron-list" bash "$install" --stage cron >"$temp_root/cron.out" 2>"$temp_root/cron.err" || fail 'Project cron install'
[[ $(wc -l < "$temp_root/cron-list") == 1 ]] || fail 'Project cron registered unexpected jobs'
HOME="$cron_home" PATH="$cron_bin:$PATH" OAOS_FAKE_CRON_LIST="$temp_root/cron-list" bash "$install" --stage cron >"$temp_root/cron-rerun.out" 2>"$temp_root/cron-rerun.err" || fail 'Project cron rerun'
[[ $(wc -l < "$temp_root/cron-list") == 1 ]] || fail 'Project cron duplicated the backup job'
printf 'PASS: Project cron registers one backup job and skips it on rerun\n'
verify_repo="$temp_root/verify-repo"
verify_home="$temp_root/verify-home"
mkdir -p "$verify_repo/editions/project" "$verify_repo/bootstrap/lib" "$verify_repo/bootstrap/verify" "$verify_home"
cp "$install" "$verify_repo/editions/project/install.sh"
cp "$repo_root/editions/project/native-stack.sh" "$verify_repo/editions/project/native-stack.sh"
cp "$repo_root/bootstrap/lib/common.sh" "$verify_repo/bootstrap/lib/common.sh"
cat > "$verify_repo/bootstrap/verify/project-verify.sh" <<'MOCK'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'Summary: PASS=4 FAIL=0 MANUAL=2 SKIP=1\n'
MOCK
HOME="$verify_home" bash "$verify_repo/editions/project/install.sh" --stage verify >"$temp_root/verify.out" 2>"$temp_root/verify.err" || fail 'Project verify stage'
python3 - "$verify_home/.oaos-install/state.json" <<'PY' || fail 'MANUAL count missing from verify state'
import json,sys
x=json.load(open(sys.argv[1], encoding='utf-8'))
assert x['stages']['verify']['status']=='done'
assert x['stages']['verify']['detail']=='Completed; MANUAL=2'
PY
grep -Fq 'MANUAL=2 item(s)' "$temp_root/verify.out" || fail 'MANUAL count missing from install log'
printf 'PASS: verify records MANUAL count in log and state\n'
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
p=re.compile(rb'''(?:sk-|ghp_|nous_|AIza|xoxb-|AKIA|vck_)[A-Za-z0-9_-]+|[0-9]{8,10}:[A-Za-z0-9_-]{35,}|Bearer[ \t]+[A-Za-z0-9._~+/=-]+|(?:api[_-]?key|token|secret|password)["']?[ \t]*[:=][ \t]*["']?(?!\[REDACTED\])[^\s"']+''',re.I)
for root in map(pathlib.Path,sys.argv[1:]):
 for f in root.rglob('*'):
  if f.is_file(): assert not p.search(f.read_bytes()),f
PY
printf 'PASS: log and state secret scan\n'
python3 - "$repo_root/editions/project" "$repo_root/bootstrap/verify/project-verify.sh" <<'PY' || fail 'native service or ingress static validation'
import pathlib,sys
root=pathlib.Path(sys.argv[1]); verify=pathlib.Path(sys.argv[2]).read_text()
native=(root/'native-stack.sh').read_text()
unit=(root/'systemd/oaos-outline.service').read_text()
install=(root/'install.sh').read_text()
for term in ('native_postgres', 'native_redis', 'native_mattermost', 'native_outline', 'CREATE DATABASE mattermost', 'CREATE DATABASE outline', 'bind 127.0.0.1', 'v1.10.1', 'patch-outline-bind.py', 'build/server/main.js'):
 assert term in native,term
for term in ('User=outline', 'EnvironmentFile=/etc/oaos/outline.env', 'ExecStart=/usr/bin/node /opt/outline/build/server/index.js', 'Restart=on-failure'):
 assert term in unit,term
assert 'native_outline_config' in install and '/opt/mattermost/bin/mmctl --local' in install
assert "labels+=('Native services')" in verify and "labels+=('Outline bind')" in verify
assert '127.0.0.1:8065' in (root/'ingress/chat.conf').read_text()
assert '127.0.0.1:3000' in (root/'ingress/note.conf').read_text()
assert 'Upgrade $http_upgrade' in (root/'ingress/chat.conf').read_text()
PY
printf 'PASS: native service and ingress static validation\n'
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
