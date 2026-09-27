#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"
# shellcheck source=bootstrap/lib/platform.sh
. "$repo_root/bootstrap/lib/platform.sh"

json=0
offline=0
while (($#)); do
  case $1 in
    --json) json=1 ;;
    --offline) offline=1 ;;
    --help|-h)
      printf 'Usage: bash bootstrap/verify/personal-verify.sh [--json] [--offline] [--help]\nExit: 0 if every automated check passes; 1 if any fails. MANUAL checks need human evidence.\n'
      exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 1 ;;
  esac
  shift
done
os_detect >/dev/null || exit 3

home=$(platform_oaos_home)
hermes_home=$(platform_hermes_home)
labels=('Hermes health' 'Model responds' 'Gateway service' 'Boot survival' 'Chat round-trip' 'Allowlist enforced' 'Wiki repository' 'Scheduled jobs' 'Config files' 'Secret hygiene' 'Backup works' 'Host capacity')
statuses=()
evidence=()
record() { statuses+=("$1"); evidence+=("$2"); }

# 1: Never print doctor output; it may contain configuration details.
if ! have_cmd hermes; then
  record FAIL 'Hermes is unavailable'
else
  doctor_dir=$(mktemp -d) || exit 3
  trap 'rm -rf -- "$doctor_dir"' EXIT
  doctor_code=0
  hermes doctor > "$doctor_dir/raw" 2>&1 || doctor_code=$?
  awk '{ gsub(sprintf("%c", 27) "\\[[0-9;]*[A-Za-z]", ""); print }' "$doctor_dir/raw" > "$doctor_dir/clean"
  if ((doctor_code == 0)); then
    record PASS 'doctor reported no issues'
  elif ! grep -Eq 'All checks passed|issue\(s\) to address' "$doctor_dir/clean"; then
    record FAIL 'doctor exited with an error and no summary block'
  elif grep -Fq '✗' "$doctor_dir/clean"; then
    record FAIL 'doctor summary contains a failed check (✗)'
  else
    record PASS 'doctor reported advisory findings only (summary present; no ✗ checks)'
  fi
  rm -rf -- "$doctor_dir"
fi

# 2: A real model request is not an offline check.
if ((offline)); then record SKIP 'Offline mode: model request not sent'
elif have_cmd hermes && platform_chat_probe; then
  record PASS 'Model reply contained OK'
else record FAIL 'Model request failed or reply did not contain OK'; fi

# 3: An absent sandbox service file is definitive; avoid touching host services.
if ! platform_gateway_service_present; then record FAIL 'Gateway registration absent'
elif have_cmd hermes && platform_gateway_status; then
  record PASS 'hermes gateway status reports running'
else record FAIL 'Gateway status is not running'; fi

# 4: Verify the user's service enablement and lingering without changing either.
if platform_gateway_autostart check; then
  if [[ $OAOS_PLATFORM == linux ]]; then record PASS 'Gateway is enabled and user lingering is yes'
  else record MANUAL 'Gateway registration and running state read back; confirm logout/login persistence'; fi
else record FAIL 'Gateway autostart registration or running state absent'; fi

# 5–6: These need messages from real accounts.
record MANUAL 'Send a message from the allowed account and confirm a reply'
record MANUAL 'Send from a disallowed account and confirm no reply'

# 7: A seed commit proves a usable repository, without exposing its contents.
wiki_native=$(oaos_native_path "$home/data/wiki") || exit 3
if [[ -d $home/data/wiki/.git ]] && git -C "$wiki_native" log -1 --format=%h >/dev/null 2>&1; then
  record PASS 'Wiki git history contains a commit'
else record FAIL 'Wiki git repository or seed commit absent'; fi

# 8: Only job names and enabled state count as evidence.
if have_cmd hermes; then
  jobs=$(hermes cron list 2>/dev/null || true)
else jobs=''; fi
if grep -Fq 'oaos-daily-backup' <<< "$jobs" && grep -Fq 'oaos-gateway-watchdog' <<< "$jobs"; then
  record PASS 'Both named jobs appear in the enabled cron list'
else record FAIL 'One or both enabled cron jobs are absent'; fi

# 9: Config files must exist and contain no token-like values.
config_files=("$hermes_home/SOUL.md" "$hermes_home/memories/USER.md" "$hermes_home/memories/MEMORY.md")
missing=0
for file in "${config_files[@]}"; do [[ -f $file ]] || missing=1; done
if ((missing)); then record FAIL 'SOUL, USER, or MEMORY file absent'
elif grep -Eq '(sk-|ghp_)[A-Za-z0-9_-]+|[0-9]{8,10}:[A-Za-z0-9_-]{35,}' "${config_files[@]}"; then
  record FAIL 'Secret-like value found in a harness file (content hidden)'
else record PASS 'SOUL, USER, MEMORY exist; no token-like values found'; fi

# 10: Keep these patterns in sync with common.sh redact; never print matches.
if [[ ! -e $home/.oaos && ! -e $home/.oaos-install ]]; then
  record SKIP 'Scan targets absent; nothing to scan'
elif ! platform_python >/dev/null 2>&1; then
  record FAIL 'Python 3 unavailable; secret scan not run'
else
  hits=$(oaos_python - "$(oaos_native_path "$home/.oaos")" "$(oaos_native_path "$home/.oaos-install")" <<'PY'
import pathlib, re, sys
pattern = re.compile(
    rb'''(?:sk-|ghp_|nous_|AIza|xoxb-|AKIA|vck_)[A-Za-z0-9_-]+
       |[0-9]{8,10}:[A-Za-z0-9_-]{35,}
       |Bearer[ \t]+[A-Za-z0-9._~+/=-]+
       |(?:api[_-]?key|token|secret|password)["']?[ \t]*[:=][ \t]*["']?(?!\[REDACTED\])[^\s"']+''',
    re.I | re.X,
)
total = 0
for root in map(pathlib.Path, sys.argv[1:]):
    if not root.exists():
        continue
    for path in root.rglob('*'):
        if 'backups' in path.relative_to(root).parts and root.name == '.oaos':
            continue
        if path.is_file():
            try:
                total += len(pattern.findall(path.read_bytes()))
            except OSError:
                total += 1
print(total)
PY
  )
  if [[ $hits == 0 ]]; then record PASS '0 secret-like matches in logs and state (backups excluded)'
  else record FAIL "$hits secret-like match(es) in logs or state; content hidden"; fi
fi

# 11: Produce a fresh backup artifact when Hermes is present.
backup_dir=$(platform_path backups)
if have_cmd hermes && [[ -d $hermes_home ]]; then
  mkdir -p "$backup_dir"
  backup="$backup_dir/hermes-verify-$(date +%Y%m%d%H%M%S)-$$.zip"
  backup_code=0
  backup_native=$(oaos_native_path "$backup") || exit 3
  hermes backup --output "$backup_native" >/dev/null 2>&1 || backup_code=$?
  if [[ ! -s $backup ]]; then
    record FAIL 'Hermes backup did not create an archive'
  elif ((backup_code == 0)); then
    record PASS 'Hermes backup created a nonempty archive'
  else
    record FAIL "Backup reported incomplete (exit $backup_code); archive exists but may be partial"
  fi
  if [[ -s $backup ]]; then
    platform_backup_prune "$backup_dir" || record FAIL 'Backup pruning blocked; Python 3 unavailable'
  fi
else record FAIL 'Hermes or its data directory is absent; backup not run'; fi

# 12: Disk margin and swap requirement are measurable without modification.
capacity='' available='' ram_kb='' swap_count=''
read -r capacity available < <(platform_disk_kib "$(platform_oaos_home)" || true)
ram_kb=$(platform_memory_kib || true)
swap_count=$(platform_swap_state || true)
if [[ $capacity =~ ^[0-9]+$ && $available =~ ^[0-9]+$ && $ram_kb =~ ^[0-9]+$ && $swap_count =~ ^[0-9]+$ ]] &&
   ((available * 100 > capacity * 10)) && { ((ram_kb >= 16777216)) || ((swap_count > 0)); }; then
  record PASS 'Disk free >10%; swap requirement met'
else record FAIL 'Disk free <=10% or swap absent on host with <16 GB RAM'; fi

pass=0 fail=0 manual=0 skip=0
for status in "${statuses[@]}"; do
  case $status in PASS) ((pass+=1));; FAIL) ((fail+=1));; MANUAL) ((manual+=1));; SKIP) ((skip+=1));; esac
done

if ((json)) && ! platform_python >/dev/null 2>&1; then
  printf 'Python 3 unavailable; --json output fell back to the table.\n' >&2
  json=0
fi
if ((json)); then
  for i in "${!labels[@]}"; do
    printf '%s\t%s\t%s\t%s\n' "$((i+1))" "${labels[$i]}" "${statuses[$i]}" "${evidence[$i]}"
  done | oaos_python -c '
import json, sys
rows = []
for line in sys.stdin:
    number, label, status, evidence = line.rstrip("\n").split("\t", 3)
    rows.append(dict(number=int(number), check=label, status=status, evidence=evidence))
summary = {key: sum(row["status"] == key for row in rows) for key in ("PASS", "FAIL", "MANUAL", "SKIP")}
print(json.dumps({"checks": rows, "summary": summary}, ensure_ascii=False))
'
else
  printf '%-3s %-22s %-7s %s\n' '#' CHECK RESULT EVIDENCE
  for i in "${!labels[@]}"; do
    printf '%-3s %-22s %-7s %s\n' "$((i+1))" "${labels[$i]}" "${statuses[$i]}" "${evidence[$i]}"
  done
  printf 'Summary: PASS=%s FAIL=%s MANUAL=%s SKIP=%s\n' "$pass" "$fail" "$manual" "$skip"
fi
((fail == 0))
