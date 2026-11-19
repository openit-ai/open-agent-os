#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Run with: bash bootstrap/tests/project-to-company-migrate-smoke.sh
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
script="$repo_root/bootstrap/migrate/project-to-company.sh"

mkdir -p "$temp_root/home" "$temp_root/bin" \
  "$temp_root/source/data/wiki/.git" "$temp_root/source/.hermes/memories" "$temp_root/source/.hermes/scripts"
printf 'index\n' > "$temp_root/source/data/wiki/index.md"
printf 'soul\n' > "$temp_root/source/.hermes/SOUL.md"
printf 'user\n' > "$temp_root/source/.hermes/memories/USER.md"
printf 'memory\n' > "$temp_root/source/.hermes/memories/MEMORY.md"
printf 'model: cheap\n' > "$temp_root/source/.hermes/config.yaml"
printf 'secret\n' > "$temp_root/source/.hermes/scripts/run.sh"
printf 'TOKEN=leaked\n' > "$temp_root/source/.hermes/.env"
export OAOS_FAKE_SOURCE="$temp_root/source"

cat > "$temp_root/bin/ssh" <<'MOCK'
#!/usr/bin/env bash
set -Eeuo pipefail
cmd=${!#}
if [[ $cmd == test\ -[ed]* ]]; then
  rel=${cmd#*\"\$HOME/}
  rel=${rel%\"}
  test "${cmd:5:2}" "${OAOS_FAKE_SOURCE:?}/$rel"
  exit $?
fi
if [[ $cmd == 'python3 -I - "$HOME/oaos/portal/portal.db"' ]]; then
  exec python3 -I - "${OAOS_FAKE_SOURCE:?}/oaos/portal/portal.db"
fi
exit 1
MOCK
# Real rsync on local paths: only the remote spec user@host:~/ is mapped to the
# fake source tree, so trailing-slash semantics are exercised for real.
cat > "$temp_root/bin/rsync" <<'MOCK'
#!/usr/bin/env bash
set -Eeuo pipefail
args=()
while (($#)); do
  case $1 in
    -e) shift 2 ;;
    *:~/*) args+=("${OAOS_FAKE_SOURCE:?}/${1#*:~/}"); shift ;;
    *) args+=("$1"); shift ;;
  esac
done
exec /usr/bin/rsync "${args[@]}"
MOCK
chmod 700 "$temp_root/bin/ssh" "$temp_root/bin/rsync"

HOME="$temp_root/home" bash "$script" --help | grep -F 'project-to-company.sh' >/dev/null || fail 'help usage'
env -u HOME bash "$script" --help > /dev/null || fail 'help should not require HOME'
code=0
HOME="$temp_root/home" bash "$script" > /dev/null 2>&1 || code=$?
[[ $code == 1 ]] || fail 'missing --source must exit 1'
for bad in 'user@' '@host:22' 'user@host extra'; do
  code=0
  HOME="$temp_root/home" bash "$script" --source "$bad" > /dev/null 2>&1 || code=$?
  [[ $code == 1 ]] || fail "invalid source rejected: $bad"
done
code=0
HOME="$temp_root/home" bash "$script" --source user@host --reverse > /dev/null 2>&1 || code=$?
[[ $code == 1 ]] || fail 'reverse conversion must be rejected'
printf 'PASS: help and argument contract\n'

HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" bash "$script" --source user@host --dry-run > "$temp_root/dry"
grep -Fq 'PLAN: user@host:~/data/wiki' "$temp_root/dry" || fail 'dry-run wiki plan'
grep -Fq 'PLAN: user@host:~/.hermes/memories/MEMORY.md' "$temp_root/dry" || fail 'dry-run memory plan'
grep -Fiq 'reverse' "$temp_root/dry" || fail 'dry-run must note reverse is out of scope'
grep -Fq 'credentials' "$temp_root/dry" || fail 'dry-run must note credential exclusion'
[[ ! -e $temp_root/home/data ]] || fail 'dry-run changed HOME'
printf 'PASS: dry-run plan without changes\n'

HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" bash "$script" --source user@host > "$temp_root/run"
[[ -f $temp_root/home/data/wiki/index.md ]] || fail 'wiki must be copied'
[[ ! -e $temp_root/home/data/wiki/wiki ]] || fail 'wiki directory was nested inside itself'
[[ -d $temp_root/home/data/wiki/.git ]] || fail 'wiki directory contents (including .git) must be copied'
[[ -f $temp_root/home/.hermes/SOUL.md ]] || fail 'SOUL must be copied'
[[ -f $temp_root/home/.hermes/memories/USER.md ]] || fail 'USER must be copied'
[[ -f $temp_root/home/.hermes/memories/MEMORY.md ]] || fail 'MEMORY must be copied'
[[ ! -e $temp_root/home/.hermes/.env ]] || fail '.env must never be copied'
[[ ! -e $temp_root/home/.hermes/scripts/run.sh ]] || fail 'non-scoped files must not be copied'
[[ ! -e $temp_root/home/.hermes/config.yaml ]] || fail 'settings excluded without --include-config'
[[ -f $temp_root/source/.hermes/.env ]] || fail 'source must be left unchanged'
printf 'PASS: wiki and memory copy excludes credentials\n'

HOME="$temp_root/home" PATH="$temp_root/bin:$PATH" bash "$script" --source user@host --include-config > /dev/null
[[ -f $temp_root/home/.hermes/config.yaml ]] || fail 'settings copied with --include-config'
printf 'PASS: settings copied with --include-config\n'

# ADR 0002: approved Project portal applicants become Company member candidates (0600).
grep -Fq 'member-candidates.json' "$temp_root/dry" || fail 'dry-run must plan the portal candidates export'
[[ ! -e $temp_root/home/.config/oaos-company/member-candidates.json ]] || fail 'candidates written without a portal database'
mkdir -p "$temp_root/source/oaos/portal"
HOME="$temp_root/source" OAOS_PORTAL_HOME="$temp_root/source/oaos/portal" python3 -I "$repo_root/editions/project/portal/server.py" --init
python3 - "$temp_root/source/oaos/portal/portal.db" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute("INSERT INTO requests(id, created_at, name, email, org, purpose, state, decided_at, mattermost_user_id)"
           " VALUES ('r1', 1, 'Kim', 'kim@example.com', 'OpenIT', 'x', 'approved', 2, ?)", ("n" * 26,))
db.execute("INSERT INTO requests(id, created_at, name, email, org, purpose, state) VALUES ('r2', 1, 'Lee', 'lee@example.com', '', 'x', 'rejected')")
db.execute("INSERT INTO decisions(request_id, decider_id, decision, decided_at) VALUES ('r1', ?, 'approved', 2)", ("a" * 26,))
db.commit()
PY
HOME="$temp_root/home-candidates" PATH="$temp_root/bin:$PATH" bash "$script" --source user@host > "$temp_root/candidates-run"
candidates="$temp_root/home-candidates/.config/oaos-company/member-candidates.json"
[[ $(stat -c '%a' "$candidates") == 600 && $(stat -c '%a' "$temp_root/home-candidates/.config/oaos-company") == 700 ]] || fail 'candidates file modes'
python3 - "$candidates" <<'PY' || fail 'candidates content'
import json, sys
doc = json.load(open(sys.argv[1]))
assert [c["email"] for c in doc["candidates"]] == ["kim@example.com"]
assert doc["candidates"][0]["state"] == "invited" and doc["candidates"][0]["project_user_id"] == "n" * 26
PY
grep -Fq 'Member candidates from the Project portal: 1' "$temp_root/candidates-run" || fail 'candidate count not reported'
rm -rf "$temp_root/source/oaos"
printf 'PASS: approved portal applicants exported as member candidates\n'

mkdir -p "$temp_root/home2/data/wiki"
printf 'old\n' > "$temp_root/home2/data/wiki/index.md"
HOME="$temp_root/home2" PATH="$temp_root/bin:$PATH" bash "$script" --source user@host > "$temp_root/backup-run"
backup=$(find "$temp_root/home2/data" -maxdepth 2 -name 'wiki.backup-*' -print -quit)
[[ -n $backup && -f $backup/index.md ]] || fail 'existing target must be preserved as .backup-*'
[[ $(cat "$temp_root/home2/data/wiki/index.md") == 'index' ]] || fail 'new wiki content must replace the backup copy'
grep -Fq 'Recovery point' "$temp_root/backup-run" || fail 'recovery point must be reported'
printf 'PASS: existing targets preserved as backups\n'

rm -f "$temp_root/source/.hermes/memories/MEMORY.md"
code=0
HOME="$temp_root/home3" PATH="$temp_root/bin:$PATH" bash "$script" --source user@host > /dev/null 2>&1 || code=$?
[[ $code == 3 ]] || fail 'missing source must exit 3'
printf 'PASS: missing source is blocked with exit 3\n'
printf 'All project-to-company migration checks passed.\n'
