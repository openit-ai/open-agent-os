#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
export HOME="$temp_root/home"
mkdir -p "$HOME/.config/systemd/user/default.target.wants" "$HOME/.hermes/memories" "$HOME/data/wiki" "$HOME/.oaos" "$temp_root/bin"
printf '[Service]\n' > "$HOME/.config/systemd/user/hermes-gateway.service"
ln -s ../../hermes-gateway.service "$HOME/.config/systemd/user/default.target.wants/hermes-gateway.service"
for file in "$HOME/.hermes/SOUL.md" "$HOME/.hermes/memories/USER.md" "$HOME/.hermes/memories/MEMORY.md"; do
  printf 'test document\n' > "$file"
done
git init -q -b main "$HOME/data/wiki"
printf 'wiki\n' > "$HOME/data/wiki/index.md"
git -C "$HOME/data/wiki" add index.md
git -C "$HOME/data/wiki" -c user.name=Test -c user.email=test@localhost commit -qm seed
cat > "$temp_root/bin/hermes" <<'SH'
#!/usr/bin/env bash
case $1 in
  doctor) exit 0 ;;
  gateway) [[ $2 == status ]] && { printf 'running\n'; exit 0; } ;;
  cron) [[ $2 == list ]] && { printf 'oaos-daily-backup\noaos-gateway-watchdog\n'; exit 0; } ;;
  backup) [[ $2 == --output ]] && { printf 'mock archive\n' > "$3"; exit 0; } ;;
  chat) printf 'OK\n'; exit 0 ;;
esac
exit 1
SH
cat > "$temp_root/bin/loginctl" <<'SH'
#!/usr/bin/env bash
printf 'yes\n'
SH
chmod 700 "$temp_root/bin/hermes" "$temp_root/bin/loginctl"
export PATH="$temp_root/bin:$PATH"

code=0
bash "$repo_root/bootstrap/verify/personal-verify.sh" --json --offline > "$temp_root/result.json" || code=$?
python3 - "$temp_root/result.json" "$code" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    result = json.load(f)
rows = result['checks']
assert len(rows) == 12
assert [row['number'] for row in rows] == list(range(1, 13))
for number in (1, 3, 4, 7, 8, 9, 10, 11):
    assert rows[number - 1]['status'] == 'PASS', rows[number - 1]
assert rows[1]['status'] == 'SKIP'
assert rows[4]['status'] == rows[5]['status'] == 'MANUAL'
assert rows[11]['status'] in ('PASS', 'FAIL')
assert int(sys.argv[2]) == (0 if rows[11]['status'] == 'PASS' else 1)
assert sum(result['summary'].values()) == 12
PY
backups=("$HOME"/.oaos/backups/hermes-verify-*.zip)
[[ -s ${backups[0]} ]] || { printf 'FAIL: mock backup absent\n' >&2; exit 1; }
printf 'PASS: Personal verifier kept 12 JSON checks, offline SKIP, gateway read-back, and backup\n'

# The model probe uses a bounded subprocess and accepts only the expected reply.
# shellcheck source=bootstrap/lib/platform.sh
. "$repo_root/bootstrap/lib/platform.sh"
OAOS_PLATFORM=linux
platform_chat_probe || { printf 'FAIL: model probe mock reply\n' >&2; exit 1; }
printf 'PASS: bounded model probe accepts mock reply\n'
