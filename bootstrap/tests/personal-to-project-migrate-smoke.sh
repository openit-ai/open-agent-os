#!/usr/bin/env bash
# Run with: bash bootstrap/tests/personal-to-project-migrate-smoke.sh
# The Personal "host" is a local directory. A stub ssh runs commands there, so the
# real rsync exercises both the --source (SSH) and --source-root (local) paths.
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
for name in ${!OAOS_@}; do unset "$name"; done
unset HERMES_HOME
fail() { printf 'FAIL: %s\n' "$1" >&2; for f in "$temp_root"/*.out "$temp_root"/*.err; do [[ -f $f ]] && { printf -- '--- %s\n' "$f" >&2; cat "$f" >&2; }; done; exit 1; }
script="$repo_root/bootstrap/migrate/personal-to-project.sh"
command -v rsync >/dev/null || { printf 'SKIP: rsync unavailable\n'; exit 0; }

make_source() {
  local src=$1
  mkdir -p "$src/data/wiki" "$src/phermes/memories" "$src/phermes/skills/demo" "$src/phermes/scripts"
  git init -q -b main "$src/data/wiki"
  printf 'index\n' > "$src/data/wiki/index.md"
  git -C "$src/data/wiki" add index.md
  git -C "$src/data/wiki" -c user.name=T -c user.email=t@localhost commit -qm seed
  # The Personal installer's relay: a bare ~/data/wiki.git that is the wiki's origin.
  git init -q --bare -b main "$src/data/wiki.git"
  git -C "$src/data/wiki" remote add origin "$src/data/wiki.git"
  git -C "$src/data/wiki" push -q -u origin main
  git -C "$src/data/wiki" remote add mirror 'ssh://relay.example/personal-wiki.git'
  printf 'soul\n' > "$src/phermes/SOUL.md"
  printf 'agents\n' > "$src/phermes/AGENTS.md"
  printf 'user\n' > "$src/phermes/memories/USER.md"
  printf 'memory\n' > "$src/phermes/memories/MEMORY.md"
  printf 'extra\n' > "$src/phermes/memories/notes.md"
  printf 'skill\n' > "$src/phermes/skills/demo/SKILL.md"
  printf 'personal shared\n' > "$src/phermes/skills/shared.md"
  printf 'job\n' > "$src/phermes/scripts/my-job.sh"
  printf 'personal backup\n' > "$src/phermes/scripts/oaos-daily-backup.sh"
  printf 'model: personal\n' > "$src/phermes/config.yaml"
  printf 'TELEGRAM_BOT_TOKEN=leaked\n' > "$src/phermes/.env"
}
make_target() {
  local home=$1
  mkdir -p "$home/data/wiki" "$home/.hermes/skills" "$home/.hermes/scripts" "$home/.hermes/memories"
  printf 'old wiki\n' > "$home/data/wiki/old.md"
  printf 'old soul\n' > "$home/.hermes/SOUL.md"
  printf 'project shared\n' > "$home/.hermes/skills/shared.md"
  printf 'project backup\n' > "$home/.hermes/scripts/oaos-daily-backup.sh"
  printf 'model: project\n' > "$home/.hermes/config.yaml"
}

# Stub ssh: drop options, take the host, run the command with HOME at the fake source.
mkdir -p "$temp_root/bin"
cat > "$temp_root/bin/ssh" <<'MOCK'
#!/usr/bin/env bash
while (($#)); do
  case $1 in -o|-l|-p) shift 2 ;; --) shift ;; -*) shift ;; *) break ;; esac
done
shift
cd "${FAKE_SOURCE_HOME:?}" && HOME=$FAKE_SOURCE_HOME HERMES_HOME="$FAKE_SOURCE_HOME/phermes" exec bash -c "$*"
MOCK
cat > "$temp_root/bin/hermes" <<'MOCK'
#!/usr/bin/env bash
case $1:$2 in
  gateway:status) if [[ -f $MOCK_GATEWAY_FLAG ]]; then printf 'running\n'; else printf 'not running\n'; fi ;;
  gateway:stop) printf 'stop\n' >> "$MOCK_GATEWAY_LOG"; rm -f "$MOCK_GATEWAY_FLAG" ;;
  gateway:start) printf 'start\n' >> "$MOCK_GATEWAY_LOG"; : > "$MOCK_GATEWAY_FLAG" ;;
  *) exit 1 ;;
esac
MOCK
chmod 700 "$temp_root/bin/"*

check_target() {
  local home=$1 label=$2
  [[ -f $home/data/wiki/index.md && ! -e $home/data/wiki/wiki ]] || fail "$label: wiki nested or absent"
  [[ -z $(git -C "$home/data/wiki" remote) ]] || fail "$label: Personal relay remote kept"
  compgen -G "$home/data/wiki.backup-*/old.md" >/dev/null || fail "$label: old wiki not preserved"
  [[ $(cat "$home/.hermes/SOUL.md") == soul && $(cat "$home/.hermes/AGENTS.md") == agents ]] || fail "$label: SOUL/AGENTS"
  [[ -f $home/.hermes/memories/notes.md && $(cat "$home/.hermes/memories/USER.md") == user ]] || fail "$label: memories"
  [[ -f $home/.hermes/skills/demo/SKILL.md && $(cat "$home/.hermes/skills/shared.md") == 'project shared' ]] || fail "$label: skills merge"
  [[ -f $home/.hermes/scripts/my-job.sh && $(cat "$home/.hermes/scripts/oaos-daily-backup.sh") == 'project backup' ]] || fail "$label: scripts merge"
  [[ $(cat "$home/.hermes/config.yaml") == 'model: project' ]] || fail "$label: config copied without --include-config"
  [[ ! -e $home/.hermes/.env ]] || fail "$label: .env copied"
  [[ -z $(find "$home" -name '*.incoming-*' -print -quit) ]] || fail "$label: staging left behind"
}

# SSH path with a running gateway: stopped for the swap and started again.
src="$temp_root/src"; make_source "$src"
home="$temp_root/project"; make_target "$home"
: > "$temp_root/gateway.flag"
# shellcheck disable=SC2088 # The literal ~/ is resolved by the script against the source HOME.
HOME="$home" PATH="$temp_root/bin:$PATH" FAKE_SOURCE_HOME="$src" MOCK_GATEWAY_FLAG="$temp_root/gateway.flag" MOCK_GATEWAY_LOG="$temp_root/gateway.log" \
  bash "$script" --source me@personal.example --source-hermes-home '~/phermes' --dry-run > "$temp_root/dry.out" 2> "$temp_root/dry.err" || fail 'dry run'
grep -Fq 'PLAN dir-replace me@personal.example:' "$temp_root/dry.out" || fail 'dry-run plan'
grep -Fq 'PLAN Personal relay ~/data/wiki.git stays on the Personal host' "$temp_root/dry.out" || fail 'dry-run plan omits the relay'
[[ ! -e $home/.hermes/AGENTS.md ]] || fail 'dry run changed target'
HOME="$home" PATH="$temp_root/bin:$PATH" FAKE_SOURCE_HOME="$src" MOCK_GATEWAY_FLAG="$temp_root/gateway.flag" MOCK_GATEWAY_LOG="$temp_root/gateway.log" \
  bash "$script" --source me@personal.example > "$temp_root/ssh.out" 2> "$temp_root/ssh.err" || fail 'ssh migration'
check_target "$home" ssh
grep -Fq 'Removed wiki remote origin (the Personal relay ~/data/wiki.git' "$temp_root/ssh.out" || fail 'relay remote removal not named'
grep -Fq 'Removed wiki remote mirror (copied from the Personal wiki)' "$temp_root/ssh.out" || fail 'other remote removal not reported'
[[ ! -e $home/data/wiki.git ]] || fail 'Personal relay was copied to the Project host'
[[ -z $(git -C "$home/data/wiki" config --get-regexp '^branch\..*\.remote$' || true) ]] || fail 'branch tracking still points at a removed remote'
[[ $(tr '\n' ' ' < "$temp_root/gateway.log") == 'stop start ' && -f $temp_root/gateway.flag ]] || fail 'gateway not stopped and restarted'
printf 'PASS: SSH migration copies wiki contents (no nesting), harness, memories, skills, scripts; drops relay and other remotes, never copies the relay; restarts gateway\n'

# Local source root with --include-config and a new wiki remote; gateway not running.
home2="$temp_root/project2"; make_target "$home2"
rm -f "$temp_root/gateway.flag" "$temp_root/gateway.log"
HOME="$home2" PATH="$temp_root/bin:$PATH" MOCK_GATEWAY_FLAG="$temp_root/gateway.flag" MOCK_GATEWAY_LOG="$temp_root/gateway.log" \
  bash "$script" --source-root "$src" --source-hermes-home "$src/phermes" --include-config --wiki-remote 'ssh://git@example.com/team-wiki.git' \
  > "$temp_root/local.out" 2> "$temp_root/local.err" || fail 'local migration'
[[ $(cat "$home2/.hermes/config.yaml") == 'model: personal' ]] || fail 'config not copied with --include-config'
[[ $(git -C "$home2/data/wiki" remote get-url origin) == 'ssh://git@example.com/team-wiki.git' ]] || fail 'wiki remote not rewritten'
[[ ! -e $temp_root/gateway.log ]] || fail 'stopped gateway was touched'
printf 'PASS: local source root migrates config on request and rewrites the wiki remote\n'

# A missing required source aborts before any target change.
home3="$temp_root/project3"; make_target "$home3"
rm -f "$src/phermes/SOUL.md"
code=0
HOME="$home3" PATH="$temp_root/bin:$PATH" MOCK_GATEWAY_FLAG="$temp_root/gateway.flag" MOCK_GATEWAY_LOG="$temp_root/gateway.log" \
  bash "$script" --source-root "$src" --source-hermes-home "$src/phermes" > "$temp_root/missing.out" 2> "$temp_root/missing.err" || code=$?
[[ $code == 3 && $(cat "$home3/.hermes/SOUL.md") == 'old soul' && -f $home3/data/wiki/old.md ]] || fail 'missing source changed the target'
printf 'PASS: missing required source exits 3 with targets unchanged\n'
