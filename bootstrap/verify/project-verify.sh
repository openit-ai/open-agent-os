#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"
# shellcheck source=bootstrap/lib/platform.sh
. "$repo_root/bootstrap/lib/platform.sh"
# shellcheck source=bootstrap/lib/secret.sh
. "$repo_root/bootstrap/lib/secret.sh"
# shellcheck source=bootstrap/lib/mail.sh
. "$repo_root/bootstrap/lib/mail.sh"
# shellcheck source=editions/project/lib.sh
. "$repo_root/editions/project/lib.sh"

json=0
offline=0
confirmed=' '
confirm_items='reboot|chat|allowlist|mm-chat|mm-deny|outline-page|mail-flow'
usage() {
  cat <<'EOF'
Usage: bash bootstrap/verify/project-verify.sh [--json] [--offline] [--confirm item[,item...]] [--help]

Run on the Project server as the non-root administrator who ran the installer
(non-interactive sudo is used for root-only reads: certificates, nginx -t, UFW,
service file modes, PostgreSQL settings, and the backup job's database dump).
--offline  skip checks that leave this host (model request, Telegram getMe, public DNS,
           HTTPS, IMAP/SMTP) as SKIP.
--confirm  record human evidence for MANUAL checks you have just observed:
           reboot        gateway answered after a safe reboot
           chat          allowed Telegram account got a reply
           allowlist     another Telegram account got none
           mm-chat       allowed Mattermost member got a reply from the bot
           mm-deny       a Mattermost member outside MATTERMOST_ALLOWED_USERS got none
           outline-page  the agent created an Outline page under its own account
           mail-flow     one message sent and one received through the bot mailbox
Exit: 0 = every check PASS (SKIP allowed); 2 = no FAIL but MANUAL checks pending;
      1 = one or more FAIL; 3 = the verifier could not run.
EOF
}
while (($#)); do
  case $1 in
    --json) json=1 ;;
    --offline) offline=1 ;;
    --confirm)
      (($# >= 2)) || { usage >&2; exit 1; }
      [[ $2 =~ ^($confirm_items)(,($confirm_items))*$ ]] || { printf 'Unknown --confirm item: %s\n' "$2" >&2; exit 1; }
      confirmed+="${2//,/ } "
      shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 1 ;;
  esac
  shift
done
os_detect >/dev/null || exit 3
[[ $OAOS_PLATFORM == linux ]] || { printf 'BLOCKED: the Project verifier runs on the Ubuntu server.\n' >&2; exit 3; }

home=$(oaos_home)
hermes_home=$(platform_hermes_home)
labels=()
statuses=()
evidence=()
record() { labels+=("$1"); statuses+=("$2"); evidence+=("$3"); }
manual_or_confirmed() {
  if [[ $confirmed == *" $2 "* ]]; then record "$1" PASS "Owner confirmed: $3"
  else record "$1" MANUAL "$4"; fi
}
cleanup_verify_temp() { [[ -z ${doctor_dir:-} ]] || rm -rf -- "$doctor_dir"; }
trap cleanup_verify_temp EXIT
config_value() { hermes config get --raw "$1" 2>/dev/null || true; }
# File mode and owner, read as root when the file is not readable by this account.
mode_owner() { run_root stat -c '%a %U' "$1" 2>/dev/null || true; }
have_hermes=0
have_cmd hermes && have_hermes=1
stack="$home/oaos/stack"
stack_get() {
  local value
  [[ -f $stack/.env ]] || return 0
  value=$(sed -n "s/^$1=//p" "$stack/.env")
  printf '%s' "${value%%$'\n'*}"
}

# --- Hermes core (same decisions as Personal) ----------------------------------------------

# Never print doctor output; it may contain configuration details.
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

if ((offline)); then record 'Model responds' SKIP 'Offline mode: model request not sent'
elif ((have_hermes)) && platform_chat_probe; then record 'Model responds' PASS 'Model reply contained OK'
else record 'Model responds' FAIL 'Model request failed or reply did not contain OK'; fi

# A just-installed gateway can still be starting; retry before failing.
if [[ ! -f $home/.config/systemd/user/hermes-gateway.service ]]; then record 'Gateway service' FAIL 'Gateway user service file absent'
else
  gateway_ok=0
  for _attempt in 1 2 3; do
    if ((have_hermes)) && project_gateway_running; then gateway_ok=1; break; fi
    sleep "${OAOS_VERIFY_RETRY_SLEEP:-5}"
  done
  if ((gateway_ok)); then record 'Gateway service' PASS 'hermes gateway status reports running'
  else record 'Gateway service' FAIL 'Gateway status is not running'; fi
fi

if platform_gateway_autostart check; then record 'Autostart registered' PASS 'User service enabled and lingering is yes'
else record 'Autostart registered' FAIL 'Gateway enablement or user lingering is absent'; fi
manual_or_confirmed 'Reboot survival' reboot 'gateway answered after a safe reboot' \
  'Safe reboot, then hermes gateway status; rerun with --confirm reboot'

# Gateway runtime state shows each platform adapter's connection.
for platform in telegram mattermost; do
  state=$(project_gateway_platform_state "$platform" || true)
  if [[ $state == connected ]]; then record "Gateway $platform" PASS "Gateway reports $platform connected"
  else record "Gateway $platform" FAIL "Gateway $platform state is ${state:-unknown}; check its token and restart the gateway"; fi
done

manual_or_confirmed 'Telegram chat' chat 'allowed Telegram account received a reply' \
  'Send a Telegram message from the allowed account and confirm a reply; rerun with --confirm chat'
manual_or_confirmed 'Telegram allowlist' allowlist 'a non-allowed Telegram account received no reply' \
  'Send from a non-allowed Telegram account and confirm no reply; rerun with --confirm allowlist'

if ((offline)); then record 'Telegram token' SKIP 'Offline mode: getMe not sent'
elif ((have_hermes == 0)); then record 'Telegram token' FAIL 'Hermes is unavailable'
else
  token=$(config_value TELEGRAM_BOT_TOKEN)
  if [[ -z $token || $token == null ]]; then record 'Telegram token' FAIL 'TELEGRAM_BOT_TOKEN is not set'
  elif result=$(oaos_telegram_get_me "$token"); then record 'Telegram token' PASS 'getMe accepted the bot token'
  else record 'Telegram token' FAIL "getMe failed ($result)"; fi
  token=''
fi
users=''
((have_hermes == 0)) || users=$(config_value TELEGRAM_ALLOWED_USERS)
if [[ -n $users && $users != null ]] && oaos_valid_user_ids "$users"; then
  record 'Telegram allowlist set' PASS 'TELEGRAM_ALLOWED_USERS holds numeric IDs only'
else record 'Telegram allowlist set' FAIL 'TELEGRAM_ALLOWED_USERS is missing or not a numeric ID list'; fi

if [[ -d $home/data/wiki/.git ]] && git -C "$home/data/wiki" log -1 --format=%h >/dev/null 2>&1; then
  record 'Wiki repository' PASS 'Wiki git history contains a commit'
else record 'Wiki repository' FAIL 'Wiki git repository or seed commit absent'; fi

config_files=("$hermes_home/SOUL.md" "$hermes_home/memories/USER.md" "$hermes_home/memories/MEMORY.md")
missing=0
for file in "${config_files[@]}"; do [[ -f $file ]] || missing=1; done
if ((missing)); then record 'Harness files' FAIL 'SOUL, USER, or MEMORY file absent'
elif grep -Eq '(sk-|ghp_)[A-Za-z0-9_-]+|[0-9]{8,10}:[A-Za-z0-9_-]{35,}' "${config_files[@]}"; then
  record 'Harness files' FAIL 'Secret-like value found in a harness file (content hidden)'
else record 'Harness files' PASS 'SOUL, USER, MEMORY exist; no token-like values found'; fi

# Keep these patterns in sync with common.sh redact; never print matches.
if [[ ! -e $home/.oaos && ! -e $home/.oaos-install ]]; then
  record 'Secret hygiene' SKIP 'Scan targets absent; nothing to scan'
else
  hits=$(python3 - "$home/.oaos" "$home/.oaos-install" <<'PY'
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

env_file=$(oaos_hermes_env_file) || exit 3
if [[ ! -f $env_file ]]; then record 'Hermes .env protected' FAIL 'Hermes .env is absent'
elif platform_secret_check "$env_file"; then record 'Hermes .env protected' PASS 'Owner-only mode 600 and ownership read back'
else record 'Hermes .env protected' FAIL 'Hermes .env is readable by others or owned by another account'; fi

config_yaml="$hermes_home/config.yaml"
if [[ ! -f $config_yaml ]]; then record 'Config secret-free' FAIL 'config.yaml is absent'
elif grep -Eiq '(sk-|ghp_|nous_|AIza|xoxb-|AKIA|vck_)[A-Za-z0-9_-]{8,}|[0-9]{8,10}:[A-Za-z0-9_-]{35,}|(api[_-]?key|token|secret|password)["'"'"']?[[:space:]]*:[[:space:]]*["'"'"']?[^[:space:]"'"'"'$][^[:space:]"'"'"']{7,}' "$config_yaml"; then
  record 'Config secret-free' FAIL 'Secret-like value found in config.yaml (content hidden); move it to the .env'
else record 'Config secret-free' PASS 'No secret-like values in config.yaml'; fi

if ((have_hermes == 0)); then record 'Unattended approvals' FAIL 'Hermes is unavailable'
else
  cron_mode=$(config_value approvals.cron_mode)
  single_mode=$(config_value approvals.single_query_mode)
  unattended_mode=$(config_value approvals.unattended_mode)
  if [[ $cron_mode == deny && $single_mode == deny && $unattended_mode == deny ]]; then
    record 'Unattended approvals' PASS 'approvals.cron_mode, single_query_mode and unattended_mode are deny'
  else record 'Unattended approvals' FAIL "approvals cron_mode=${cron_mode:-unset} single_query_mode=${single_mode:-unset} unattended_mode=${unattended_mode:-unset}; expected deny"; fi
fi

# --- Backup: run the real daily job, then a restore dry run on its archive ------------------

jobs=''
((have_hermes == 0)) || jobs=$(hermes cron list 2>/dev/null || true)
backup_script="$hermes_home/scripts/oaos-daily-backup.sh"
if ! grep -Fq 'oaos-daily-backup' <<< "$jobs"; then
  record 'Scheduled jobs' FAIL 'oaos-daily-backup is absent from the enabled Hermes cron list'
elif ! cmp -s "$repo_root/editions/project/backup.sh" "$backup_script"; then
  record 'Scheduled jobs' FAIL 'oaos-daily-backup.sh is not the current OAOS Project backup script; rerun the cron stage'
else record 'Scheduled jobs' PASS 'Daily backup job registered with the current Project backup script'; fi

archive=''
if [[ ! -x $backup_script ]]; then
  record 'Backup works' FAIL 'Backup script is absent; rerun the cron stage'
else
  backup_code=0
  archive=$(bash "$backup_script" 2>/dev/null | tail -n 1) || backup_code=$?
  backup_dir="$home/.oaos/backups"
  if ((backup_code != 0)) || [[ ! -s $archive ]]; then
    record 'Backup works' FAIL "Backup job failed (exit $backup_code); databases or service data may be unreadable"
    archive=''
  elif [[ $(platform_file_mode "$backup_dir") != 700 || $(platform_file_mode "$archive") != 600 ]]; then
    record 'Backup works' FAIL 'Backup directory is not 700 or the archive is not 600'
  else
    manifest=$(tar -xOf "$archive" ./manifest.txt 2>/dev/null || true)
    parts='home'
    complete=1
    if [[ -f $stack/.env ]]; then
      for item in 'database mattermost' 'database outline' 'system etc/oaos'; do
        grep -Fq "$item" <<< "$manifest" || complete=0
      done
      parts='home, PostgreSQL mattermost+outline, service data, /etc/oaos'
    fi
    if ((complete)); then record 'Backup works' PASS "Daily job archive (700/600) includes $parts; newest 7 kept"
    else record 'Backup works' FAIL 'Backup archive lacks a database dump or service data'; fi
  fi
fi
if [[ -z $archive ]]; then record 'Restore dry run' FAIL 'No fresh backup archive to test'
elif bash "$repo_root/editions/project/restore.sh" --dry-run "$archive" >/dev/null 2>&1; then
  record 'Restore dry run' PASS 'restore.sh --dry-run read every part of the new archive'
else record 'Restore dry run' FAIL 'restore.sh --dry-run rejected the new archive'; fi

# --- G0: capacity, firewall ---------------------------------------------------------------

capacity='' available=''
read -r capacity available < <(df -Pk / | awk 'NR==2 {print $2, $4}')
ram_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
swap_count=$(awk 'END {print NR-1}' /proc/swaps)
IFS='|' read -r cap_level cap_summary cap_below _ < <(project_capacity || true)
if ! ((available * 100 > capacity * 10)) || ! { ((ram_kb >= 16777216)) || ((swap_count > 0)); }; then
  record 'Host capacity' FAIL 'Disk free <=10% or swap absent on a host with <16 GB RAM'
elif [[ $cap_level == short ]]; then record 'Host capacity' FAIL "$cap_summary is below the Project minimum (2 vCPU / 8 GB / 60 GB)"
elif [[ $cap_level == warn ]]; then record 'Host capacity' PASS "Disk and swap OK; WARN below recommendation: $cap_below"
else record 'Host capacity' PASS "${cap_summary:-capacity read} meets 4 vCPU / 16 GB / 200 GB; disk and swap OK"; fi

ufw_status=$(run_root ufw status 2>/dev/null || true)
if [[ $ufw_status != *'Status: active'* ]]; then record 'Firewall' FAIL 'UFW is not active (or not readable with sudo)'
else
  missing_rules=()
  for port in 22 80 443; do
    pattern="^$port(/tcp)?[[:space:]]+ALLOW"
    [[ $port != 22 ]] || pattern="^(22(/tcp)?|OpenSSH)[[:space:]]+ALLOW"
    grep -Eq "$pattern" <<< "$ufw_status" || missing_rules+=("$port")
  done
  if ((${#missing_rules[@]})); then record 'Firewall' FAIL "UFW active but no ALLOW rule for ${missing_rules[*]}"
  else record 'Firewall' PASS 'UFW active; 22, 80 and 443 allowed'; fi
fi

# --- G6: DNS, TLS, nginx ------------------------------------------------------------------

public_ip=''
((offline)) || public_ip=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)
for item in CHAT_DOMAIN NOTE_DOMAIN PORTAL_DOMAIN; do
  domain=$(stack_get "$item")
  if ((offline)); then record "DNS $item" SKIP 'Offline mode: public IPv4 not looked up'
  else
    dns_answer=''
    [[ -z $domain ]] || dns_answer=$(getent ahostsv4 "$domain" 2>/dev/null | awk '{print $1}' || true)
    if [[ -n $domain && $public_ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && grep -Fqx "$public_ip" <<< "$dns_answer"; then
      record "DNS $item" PASS 'A record resolves to this server public IPv4'
    else record "DNS $item" FAIL 'Domain A record or public IPv4 does not match'; fi
  fi
  if [[ -n $domain ]] && run_root openssl x509 -checkend 604800 -noout -in "/etc/letsencrypt/live/$domain/fullchain.pem" >/dev/null 2>&1; then
    record "TLS $item" PASS 'Certificate valid for more than seven days'
  else record "TLS $item" FAIL 'Certificate absent or expires within seven days'; fi
  if ((offline)); then record "HTTPS $item" SKIP 'Offline mode: HTTPS request not sent'
  elif [[ -n $domain ]] && curl -fsSIL --max-time 10 "https://$domain/" >/dev/null 2>&1; then
    record "HTTPS $item" PASS 'HTTPS responds'
  else record "HTTPS $item" FAIL 'HTTPS request failed'; fi
done
if systemctl is-enabled --quiet certbot.timer 2>/dev/null || systemctl is-enabled --quiet snap.certbot.renew.timer 2>/dev/null; then
  record 'TLS auto-renew' PASS 'Certbot renewal timer enabled'
else record 'TLS auto-renew' FAIL 'Certbot renewal timer unavailable or disabled'; fi
if have_cmd nginx && run_root nginx -t >/dev/null 2>&1; then record 'nginx config' PASS 'nginx -t passed'
else record 'nginx config' FAIL 'nginx -t failed or nginx absent'; fi

# --- Native services and loopback bindings (architecture §6) -------------------------------

healthy=1
for service in postgresql redis-server mattermost oaos-outline; do
  systemctl is-active --quiet "$service" 2>/dev/null || healthy=0
done
if ((healthy)) && pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 &&
   [[ $(redis-cli -h 127.0.0.1 ping 2>/dev/null || true) == PONG ]]; then
  record 'Native services' PASS 'PostgreSQL, Redis, Mattermost and Outline systemd services are active'
else record 'Native services' FAIL 'One or more native services or local dependencies are unavailable'; fi
for port in 5432 6379 8065 3000; do
  listen_code=0
  listeners=$(platform_listeners "$port") || listen_code=$?
  if ((listen_code != 0)); then record "Loopback :$port" FAIL 'Listener tool (ss) unavailable'
  elif [[ -z $listeners ]]; then record "Loopback :$port" FAIL "No listener on port $port"
  else
    # shellcheck disable=SC2086 # One address per word.
    if platform_listeners_loopback_only $listeners; then record "Loopback :$port" PASS "Port $port listens on loopback only"
    else record "Loopback :$port" FAIL "Port $port listens on a non-loopback address"; fi
  fi
done
pg_listen=$(run_root runuser -u postgres -- psql -X -Atqc 'SHOW listen_addresses' 2>/dev/null || true)
if [[ $pg_listen == localhost || $pg_listen == 127.0.0.1 ]]; then record 'PostgreSQL bind' PASS "listen_addresses = $pg_listen"
else record 'PostgreSQL bind' FAIL "listen_addresses is ${pg_listen:-unreadable}; expected localhost"; fi
ping_body=$(curl -fsS --max-time 5 http://127.0.0.1:8065/api/v4/system/ping 2>/dev/null || true)
if [[ $ping_body == *'"status":"OK"'* ]]; then record 'Mattermost ping' PASS 'Mattermost system ping OK'
else record 'Mattermost ping' FAIL 'Mattermost system ping failed'; fi
if curl -fsS --max-time 5 http://127.0.0.1:3000/_health >/dev/null 2>&1; then record 'Outline health' PASS 'Outline health endpoint OK'
else record 'Outline health' FAIL 'Outline health endpoint failed'; fi

# Approved per-service secret files: stack .env (installer user), outline.env (root), config.json (mattermost).
modes_ok=1
modes_detail=()
[[ $(stat -c '%a %U' "$stack/.env" 2>/dev/null || true) == "600 $(id -un)" ]] || { modes_ok=0; modes_detail+=('stack/.env'); }
[[ $(mode_owner /etc/oaos/outline.env) == '600 root' ]] || { modes_ok=0; modes_detail+=('/etc/oaos/outline.env'); }
[[ $(mode_owner /opt/mattermost/config/config.json) == '600 mattermost' ]] || { modes_ok=0; modes_detail+=('config.json'); }
if ((modes_ok)); then record 'Service secret files' PASS 'stack/.env, /etc/oaos/outline.env, Mattermost config.json are 600 with expected owners'
else record 'Service secret files' FAIL "Mode or owner wrong for: ${modes_detail[*]}"; fi
leftover=()
for key in MATTERMOST_TOKEN OUTLINE_API_TOKEN OAOS_MAIL_PASSWORD; do [[ -z $(stack_get "$key") ]] || leftover+=("$key"); done
if ((${#leftover[@]})); then record 'Stack gate secrets' FAIL "${leftover[*]} still in stack/.env; rerun mail/team to move them into the Hermes .env"
else record 'Stack gate secrets' PASS 'No gate token or mail password copied into stack/.env'; fi

# --- G7 / G8 / G9 ---------------------------------------------------------------------------

mm_token=$(project_env_get MATTERMOST_TOKEN || true)
if [[ -z $mm_token ]]; then record 'Mattermost bot' FAIL 'MATTERMOST_TOKEN is not in the Hermes .env (G7)'
elif result=$(project_mattermost_bot_check "$mm_token"); then record 'Mattermost bot' PASS "Token authenticated: $result"
else record 'Mattermost bot' FAIL "Token rejected: $result"; fi
mm_users=$(project_env_get MATTERMOST_ALLOWED_USERS || true)
if [[ -z $mm_users ]] || ! project_valid_mattermost_ids "$mm_users"; then
  record 'Mattermost allowlist' FAIL 'MATTERMOST_ALLOWED_USERS is missing or not a list of Mattermost user IDs'
elif [[ -n $mm_token ]] && result=$(project_mattermost_ids_exist "$mm_token" "$mm_users"); then
  record 'Mattermost allowlist' PASS "MATTERMOST_ALLOWED_USERS set; $result"
else record 'Mattermost allowlist' FAIL "MATTERMOST_ALLOWED_USERS could not be confirmed (${result:-no bot token})"; fi
mm_token=''
manual_or_confirmed 'Mattermost chat' mm-chat 'allowed Mattermost member received a reply' \
  'An allowed member messages the bot and gets a reply; rerun with --confirm mm-chat'
manual_or_confirmed 'Mattermost deny' mm-deny 'a member outside the allowlist received no reply' \
  'A member outside MATTERMOST_ALLOWED_USERS messages the bot and gets no reply; rerun with --confirm mm-deny'

mailbox=$(project_env_get OAOS_MAIL_ADDRESS || true)
outline_token=$(project_env_get OUTLINE_API_TOKEN || true)
if [[ -z $outline_token ]]; then record 'Outline agent token' FAIL 'OUTLINE_API_TOKEN is not in the Hermes .env (G8)'
elif [[ -z $mailbox ]]; then record 'Outline agent token' FAIL 'OAOS_MAIL_ADDRESS (G9) is absent; the agent account cannot be matched'
elif result=$(project_outline_agent_check "$outline_token" "$mailbox"); then record 'Outline agent token' PASS "Token authenticated: $result"
else record 'Outline agent token' FAIL "Token rejected: $result"; fi
outline_token=''
note=$(stack_get NOTE_DOMAIN)
if [[ -n $note && $(project_env_get OUTLINE_URL || true) == "https://$note" ]]; then record 'Outline URL' PASS 'OUTLINE_URL points at the note domain'
else record 'Outline URL' FAIL 'OUTLINE_URL is absent or does not match the note domain; rerun team'; fi
manual_or_confirmed 'Outline page' outline-page 'the agent created a page under its own account' \
  'Ask the agent to create an Outline page and confirm it is authored by the agent account; rerun with --confirm outline-page'

imap=$(project_env_get OAOS_MAIL_IMAP_HOST || true)
smtp=$(project_env_get OAOS_MAIL_SMTP_HOST || true)
for kind in IMAP SMTP; do
  if [[ $kind == IMAP ]]; then host=$imap; else host=$smtp; fi
  if [[ -z $mailbox || -z $host ]] || ! oaos_secret_present OAOS_MAIL_PASSWORD; then
    record "Bot mailbox $kind" FAIL 'G9 mailbox values are absent from the Hermes .env'
  elif ((offline)); then record "Bot mailbox $kind" SKIP "Offline mode: $kind login not attempted"
  elif result=$(project_mail_login "$(tr '[:upper:]' '[:lower:]' <<< "$kind")" "$mailbox" "$host"); then
    record "Bot mailbox $kind" PASS "$kind login succeeded"
  else record "Bot mailbox $kind" FAIL "$kind login failed ($result)"; fi
done
manual_or_confirmed 'Mail flow' mail-flow 'one message sent and one received by the bot mailbox' \
  'Ask the agent to send a test mail and read a reply; rerun with --confirm mail-flow'

pass=0 fail=0 manual=0 skip=0
for status in "${statuses[@]}"; do
  case $status in PASS) ((pass+=1));; FAIL) ((fail+=1));; MANUAL) ((manual+=1));; SKIP) ((skip+=1));; esac
done

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
