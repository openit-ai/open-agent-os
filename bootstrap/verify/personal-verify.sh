#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"
# shellcheck source=bootstrap/lib/platform.sh
. "$repo_root/bootstrap/lib/platform.sh"
# shellcheck source=bootstrap/lib/secret.sh
. "$repo_root/bootstrap/lib/secret.sh"

json=0
offline=0
confirmed=' '
usage() {
  cat <<'EOF'
Usage: bash bootstrap/verify/personal-verify.sh [--json] [--offline] [--confirm item[,item...]] [--help]

--offline  skip checks that need the network (model request, Telegram getMe) as SKIP.
--confirm  record human evidence for MANUAL checks you have just observed:
           reboot (gateway answered after a safe reboot or logout/login),
           chat (allowed account got a reply), allowlist (other account got none).
Exit: 0 = every check PASS (SKIP allowed); 2 = no FAIL but MANUAL checks pending;
      1 = one or more FAIL; 3 = the verifier could not run on this platform.
EOF
}
while (($#)); do
  case $1 in
    --json) json=1 ;;
    --offline) offline=1 ;;
    --confirm)
      (($# >= 2)) || { usage >&2; exit 1; }
      [[ $2 =~ ^(reboot|chat|allowlist)(,(reboot|chat|allowlist))*$ ]] || { printf 'Unknown --confirm item: %s\n' "$2" >&2; exit 1; }
      confirmed+="${2//,/ } "
      shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 1 ;;
  esac
  shift
done
os_detect >/dev/null || exit 3

home=$(platform_oaos_home)
hermes_home=$(platform_hermes_home)
labels=()
statuses=()
evidence=()
record() { labels+=("$1"); statuses+=("$2"); evidence+=("$3"); }
manual_or_confirmed() {
  if [[ $confirmed == *" $2 "* ]]; then record "$1" PASS "Owner confirmed: $3"
  else record "$1" MANUAL "$4"; fi
}
cleanup_verify_temp() {
  [[ -z ${doctor_dir:-} ]] || rm -rf -- "$doctor_dir"
  [[ -z ${backup_output:-} ]] || rm -f -- "$backup_output"
}
trap cleanup_verify_temp EXIT
config_value() { hermes config get --raw "$1" 2>/dev/null || true; }
have_hermes=0
have_cmd hermes && have_hermes=1

# 1: Never print doctor output; it may contain configuration details.
if ((have_hermes == 0)); then
  record 'Hermes health' FAIL 'Hermes is unavailable'
else
  doctor_dir=$(mktemp -d) || exit 3
  doctor_code=0
  hermes doctor > "$doctor_dir/raw" 2>&1 || doctor_code=$?
  awk '{ gsub(sprintf("%c", 27) "\\[[0-9;]*[A-Za-z]", ""); print }' "$doctor_dir/raw" > "$doctor_dir/clean"
  if ((doctor_code == 0)); then
    record 'Hermes health' PASS 'doctor reported no issues'
  elif ! grep -Eq 'All checks passed|issue\(s\) to address' "$doctor_dir/clean"; then
    record 'Hermes health' FAIL 'doctor exited with an error and no summary block'
  elif grep -Fq '✗' "$doctor_dir/clean"; then
    record 'Hermes health' FAIL 'doctor summary contains a failed check (✗)'
  else
    record 'Hermes health' PASS 'doctor reported advisory findings only (summary present; no ✗ checks)'
  fi
  rm -rf -- "$doctor_dir"
fi

# 2: A real model request is not an offline check.
if ((offline)); then record 'Model responds' SKIP 'Offline mode: model request not sent'
elif ((have_hermes)) && platform_chat_probe; then
  record 'Model responds' PASS 'Model reply contained OK'
else record 'Model responds' FAIL 'Model request failed or reply did not contain OK'; fi

# 3: An absent service registration is definitive; avoid touching host services.
if ! platform_gateway_service_present; then record 'Gateway service' FAIL 'Gateway registration absent'
elif ((have_hermes)) && platform_gateway_status; then
  record 'Gateway service' PASS 'hermes gateway status reports running'
else record 'Gateway service' FAIL 'Gateway status is not running'; fi

# 4–5: Registration read-back is automatic; surviving a reboot or login needs a person.
if platform_gateway_autostart check; then
  case $OAOS_PLATFORM in
    linux) record 'Autostart registered' PASS 'User service enabled and lingering is yes' ;;
    macos) record 'Autostart registered' PASS 'launchd registration read back and gateway running' ;;
    *) record 'Autostart registered' PASS 'ONLOGON task or Startup fallback read back and gateway running' ;;
  esac
else record 'Autostart registered' FAIL 'Gateway autostart registration or running state absent'; fi
manual_or_confirmed 'Reboot survival' reboot 'gateway answered after a safe reboot or logout/login' \
  'Safe reboot (or logout/login) then hermes gateway status; rerun with --confirm reboot'

# 6–7: These need messages from real accounts.
manual_or_confirmed 'Chat round-trip' chat 'allowed account received a reply' \
  'Send a message from the allowed account and confirm a reply; rerun with --confirm chat'
manual_or_confirmed 'Allowlist enforced' allowlist 'a non-allowed account received no reply' \
  'Send from a disallowed account and confirm no reply; rerun with --confirm allowlist'

# 8: The token is checked with getMe; it never reaches argv or output.
if ((offline)); then record 'Telegram token' SKIP 'Offline mode: getMe not sent'
elif ((have_hermes == 0)); then record 'Telegram token' FAIL 'Hermes is unavailable'
else
  token=$(config_value TELEGRAM_BOT_TOKEN)
  if [[ -z $token || $token == null ]]; then record 'Telegram token' FAIL 'TELEGRAM_BOT_TOKEN is not set'
  elif result=$(oaos_telegram_get_me "$token"); then record 'Telegram token' PASS 'getMe accepted the bot token'
  else record 'Telegram token' FAIL "getMe failed ($result)"; fi
  token=''
fi

# 9: Telegram is default-deny only with a numeric allowlist; the IDs are not printed.
users=''
((have_hermes == 0)) || users=$(config_value TELEGRAM_ALLOWED_USERS)
if [[ -n $users && $users != null ]] && oaos_valid_user_ids "$users"; then
  record 'Allowlist configured' PASS 'TELEGRAM_ALLOWED_USERS holds numeric IDs only'
else record 'Allowlist configured' FAIL 'TELEGRAM_ALLOWED_USERS is missing or not a numeric ID list'; fi

# 10: A seed commit proves a usable repository, without exposing its contents.
wiki=$(platform_path wiki)
wiki_native=$(oaos_native_path "$wiki") || exit 3
if [[ -d $wiki/.git ]] && git -C "$wiki_native" log -1 --format=%h >/dev/null 2>&1; then
  if [[ -x $wiki/.git/hooks/pre-commit ]]; then record 'Wiki repository' PASS 'Wiki has a commit and a pre-commit hook'
  else record 'Wiki repository' PASS 'Wiki has a commit (no pre-commit secret hook installed)'; fi
else record 'Wiki repository' FAIL 'Wiki git repository or seed commit absent'; fi

# 11: Backup is a Hermes cron job; the watchdog is an OS timer outside the gateway.
jobs=''
((have_hermes == 0)) || jobs=$(hermes cron list 2>/dev/null || true)
watchdog_code=0
platform_watchdog_check || watchdog_code=$?
legacy=''
grep -Fq 'oaos-gateway-watchdog' <<< "$jobs" && legacy='; legacy Hermes watchdog job should be removed'
if ! grep -Fq 'oaos-daily-backup' <<< "$jobs"; then
  record 'Scheduled jobs' FAIL 'oaos-daily-backup is absent from the enabled Hermes cron list'
elif ((watchdog_code == 0)); then
  record 'Scheduled jobs' PASS "Backup cron job and OS watchdog timer present$legacy"
elif ((watchdog_code == 4)); then
  record 'Scheduled jobs' PASS "Backup cron job present; this lane has no OAOS watchdog (Hermes logon task only)$legacy"
else record 'Scheduled jobs' FAIL 'Backup job present but the OS watchdog timer is absent or inactive'; fi

# 12: Harness files exist, hold no token-like values, and have no template placeholders left.
config_files=("$hermes_home/SOUL.md" "$hermes_home/memories/USER.md" "$hermes_home/memories/MEMORY.md")
missing=0
for file in "${config_files[@]}"; do [[ -f $file ]] || missing=1; done
if ((missing)); then record 'Harness files' FAIL 'SOUL, USER, or MEMORY file absent'
elif grep -Eq '(sk-|ghp_)[A-Za-z0-9_-]+|[0-9]{8,10}:[A-Za-z0-9_-]{35,}' "${config_files[@]}"; then
  record 'Harness files' FAIL 'Secret-like value found in a harness file (content hidden)'
else
  placeholders=$(cat "${config_files[@]}" | { grep -o '{{' || true; } | wc -l | tr -d ' ')
  if [[ $placeholders == 0 ]]; then record 'Harness files' PASS 'SOUL, USER, MEMORY filled in; no token-like values'
  else record 'Harness files' MANUAL "$placeholders template placeholder(s) left for the seeding conversation (harness/seeding.md)"; fi
fi

# 13: Keep these patterns in sync with common.sh redact; never print matches.
if [[ ! -e $home/.oaos && ! -e $home/.oaos-install ]]; then
  record 'Secret hygiene' SKIP 'Scan targets absent; nothing to scan'
elif ! platform_python >/dev/null 2>&1; then
  record 'Secret hygiene' FAIL 'Python 3 unavailable; secret scan not run'
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
        rel = path.relative_to(root).parts
        if root.name == '.oaos' and ('backups' in rel or 'cache' in rel):
            continue
        if path.is_file():
            try:
                total += len(pattern.findall(path.read_bytes()))
            except OSError:
                total += 1
print(total)
PY
  )
  if [[ $hits == 0 ]]; then record 'Secret hygiene' PASS '0 secret-like matches in logs and state (backups excluded)'
  else record 'Secret hygiene' FAIL "$hits secret-like match(es) in logs or state; content hidden"; fi
fi

# 14: Secrets live only in the Hermes .env, owner-only (mode 600 + owner, or NTFS ACL).
env_file=$(oaos_hermes_env_file) || exit 3
if [[ ! -f $env_file ]]; then record 'Hermes .env protected' FAIL 'Hermes .env is absent'
elif platform_secret_check "$env_file"; then
  if [[ $OAOS_PLATFORM == windows-gitbash ]]; then record 'Hermes .env protected' PASS 'NTFS owner and protected ACL read back'
  else record 'Hermes .env protected' PASS 'Owner-only mode 600 and ownership read back'; fi
else record 'Hermes .env protected' FAIL 'Hermes .env is readable by others or owned by another account'; fi

# 15: config.yaml may reference ${VARS} but must not hold raw secrets.
config_yaml="$hermes_home/config.yaml"
if [[ ! -f $config_yaml ]]; then record 'Config secret-free' FAIL 'config.yaml is absent'
elif grep -Eiq '(sk-|ghp_|nous_|AIza|xoxb-|AKIA|vck_)[A-Za-z0-9_-]{8,}|[0-9]{8,10}:[A-Za-z0-9_-]{35,}|(api[_-]?key|token|secret|password)["'"'"']?[[:space:]]*:[[:space:]]*["'"'"']?[^[:space:]"'"'"'$][^[:space:]"'"'"']{7,}' "$config_yaml"; then
  record 'Config secret-free' FAIL 'Secret-like value found in config.yaml (content hidden); move it to the .env'
else record 'Config secret-free' PASS 'No secret-like values in config.yaml'; fi

# 16: Unattended runs (cron, webhooks) must deny flagged actions (architecture §6).
if ((have_hermes == 0)); then record 'Unattended approvals' FAIL 'Hermes is unavailable'
else
  cron_mode=$(config_value approvals.cron_mode)
  single_mode=$(config_value approvals.single_query_mode)
  unattended_mode=$(config_value approvals.unattended_mode)
  if [[ $cron_mode == deny && $single_mode == deny && $unattended_mode == deny ]]; then
    record 'Unattended approvals' PASS 'approvals.cron_mode, single_query_mode and unattended_mode are deny'
  else record 'Unattended approvals' FAIL "approvals cron_mode=${cron_mode:-unset} single_query_mode=${single_mode:-unset} unattended_mode=${unattended_mode:-unset}; expected deny"; fi
fi

# 17: The Dashboard, when running, listens on loopback only (use an SSH tunnel to reach it).
dashboard_port=${OAOS_DASHBOARD_PORT:-9119}
[[ $dashboard_port =~ ^[0-9]+$ ]] || dashboard_port=9119
listeners_code=0
listeners=$(platform_listeners "$dashboard_port") || listeners_code=$?
if ((listeners_code != 0)); then
  record 'Dashboard loopback' MANUAL "Listener tool unavailable; confirm port $dashboard_port is bound to 127.0.0.1 only"
elif [[ -z $listeners ]]; then
  record 'Dashboard loopback' PASS "No listener on port $dashboard_port (Dashboard not running)"
else
  # shellcheck disable=SC2086 # One address per word.
  if platform_listeners_loopback_only $listeners; then
    record 'Dashboard loopback' PASS "Port $dashboard_port listens on loopback only"
  else record 'Dashboard loopback' FAIL "Port $dashboard_port listens on a non-loopback address"; fi
fi

# 18: Produce a fresh owner-only backup artifact when Hermes is present.
backup_dir=$(platform_path backups)
if ((have_hermes)) && [[ -d $hermes_home ]]; then
  mkdir -p "$backup_dir"
  [[ $OAOS_PLATFORM == windows-gitbash ]] || chmod 700 "$backup_dir"
  backup="$backup_dir/hermes-verify-$(date +%Y%m%d%H%M%S)-$$.zip"
  backup_code=0
  backup_native=$(oaos_native_path "$backup") || exit 3
  backup_output=$(mktemp) || exit 3
  (umask 077; hermes backup --output "$backup_native") > "$backup_output" 2>&1 || backup_code=$?
  [[ ! -f $backup || $OAOS_PLATFORM == windows-gitbash ]] || chmod 600 "$backup"
  pruned=1
  if [[ -s $backup ]]; then platform_backup_prune "$backup_dir" || pruned=0; fi
  private=1
  if [[ $OAOS_PLATFORM != windows-gitbash ]]; then
    [[ $(platform_file_mode "$backup_dir") == 700 ]] || private=0
    while IFS= read -r archive; do
      [[ -z $archive || $(platform_file_mode "$archive") == 600 ]] || private=0
    done < <(find "$backup_dir" -maxdepth 1 -type f \( -name 'hermes-verify-*.zip' -o -name 'oaos-*.tar.gz' \) -print)
  fi
  if [[ ! -s $backup ]]; then
    record 'Backup works' FAIL 'Hermes backup did not create an archive'
  elif ((pruned == 0)); then
    record 'Backup works' FAIL 'Backup created but pruning was blocked (Python 3 unavailable)'
  elif ((private == 0)); then
    record 'Backup works' FAIL 'Backup directory is not 700 or an archive is not 600'
  elif ((backup_code == 0)); then
    record 'Backup works' PASS 'Hermes backup created a nonempty archive (owner-only)'
  else
    skipped_count=$(awk '
      {
        gsub(sprintf("%c", 27) "\\[[0-9;]*[A-Za-z]", "")
        if (index(tolower($0), "could not be added") == 0) next
        for (i = 1; i < NF; i++) {
          if ($i ~ /^[0-9]+$/ && tolower($(i + 1)) ~ /^file(\(s\)|s)?$/) {
            if ($i > 0) print $i
            exit
          }
        }
      }
    ' "$backup_output")
    if [[ -n $skipped_count ]]; then
      record 'Backup works' PASS "Hermes backup archive nonempty; $skipped_count unreadable file(s) skipped (permissions)"
    else
      record 'Backup works' FAIL "Backup exited $backup_code without a counted skipped-file report; archive may be partial"
    fi
  fi
  rm -f -- "$backup_output"
else record 'Backup works' FAIL 'Hermes or its data directory is absent; backup not run'; fi

# 19: Disk margin and swap requirement are measurable without modification.
capacity='' available='' ram_kb='' swap_count=''
read -r capacity available < <(platform_disk_kib "$(platform_oaos_home)" || true)
ram_kb=$(platform_memory_kib || true)
swap_count=$(platform_swap_state || true)
if [[ $capacity =~ ^[0-9]+$ && $available =~ ^[0-9]+$ && $ram_kb =~ ^[0-9]+$ && $swap_count =~ ^[0-9]+$ ]] &&
   ((available * 100 > capacity * 10)) && { ((ram_kb >= 16777216)) || ((swap_count > 0)); }; then
  record 'Host capacity' PASS 'Disk free >10%; swap requirement met'
else record 'Host capacity' FAIL 'Disk free <=10% or swap absent on host with <16 GB RAM'; fi

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
  if ((fail == 0 && manual > 0)); then
    printf 'MANUAL pending: %s check(s) need human evidence; installation is not complete until they pass.\n' "$manual"
  fi
fi
if ((fail > 0)); then exit 1; fi
if ((manual > 0)); then exit 2; fi
exit 0
