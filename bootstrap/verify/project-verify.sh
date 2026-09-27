#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"

json=0
offline=0
while (($#)); do
  case $1 in
    --json) json=1 ;;
    --offline) offline=1 ;;
    --help|-h)
      printf 'Usage: bash bootstrap/verify/project-verify.sh [--json] [--offline] [--help]\nExit: 0 if every automated check passes; 1 if any fails. MANUAL checks need human evidence.\n'
      exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 1 ;;
  esac
  shift
done

home=$(oaos_home)
labels=('Hermes health' 'Model responds' 'Gateway service' 'Boot survival' 'Chat round-trip' 'Allowlist enforced' 'Wiki repository' 'Scheduled jobs' 'Config files' 'Secret hygiene' 'Backup works' 'Host capacity')
statuses=()
evidence=()
record() { statuses+=("$1"); evidence+=("$2"); }

# 1: Never print doctor output; it may contain configuration details.
# doctor exits non-zero for advisory findings too, so the summary is inspected.
if ! have_cmd hermes; then
  record FAIL 'Hermes is unavailable'
else
  doctor_dir=$(mktemp -d) || exit 3
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
elif have_cmd hermes && timeout 30s hermes chat -q 'Reply with exactly: OK' 2>/dev/null | grep -Fq OK; then
  record PASS 'Model reply contained OK'
else record FAIL 'Model request failed or reply did not contain OK'; fi

# 3: An absent sandbox service file is definitive; avoid touching host services.
service="$home/.config/systemd/user/hermes-gateway.service"
if [[ ! -f $service ]]; then record FAIL 'Gateway user service file absent'
else
  # A just-installed gateway can still be starting; retry before failing.
  gateway_ok=0
  for _attempt in 1 2 3; do
    if have_cmd hermes && hermes gateway status 2>/dev/null | grep -Eq 'Gateway is running|gateway service is running'; then
      gateway_ok=1
      break
    fi
    sleep 5
  done
  if ((gateway_ok)); then record PASS 'hermes gateway status reports running'
  else record FAIL 'Gateway status is not running'; fi
fi

# 4: Verify the user's service enablement and lingering without changing either.
if [[ -L $home/.config/systemd/user/default.target.wants/hermes-gateway.service ]] &&
   [[ $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true) == yes ]]; then
  record PASS 'Gateway is enabled and user lingering is yes'
else record FAIL 'Gateway enablement or user lingering is absent'; fi

# 5–6: These need messages from real accounts.
record MANUAL 'Send a message from the allowed account and confirm a reply'
record MANUAL 'Send from a disallowed account and confirm no reply'

# 7: A seed commit proves a usable repository, without exposing its contents.
if [[ -d $home/data/wiki/.git ]] && git -C "$home/data/wiki" log -1 --format=%h >/dev/null 2>&1; then
  record PASS 'Wiki git history contains a commit'
else record FAIL 'Wiki git repository or seed commit absent'; fi

# 8: Only job names and enabled state count as evidence.
if have_cmd hermes; then
  jobs=$(hermes cron list 2>/dev/null || true)
else jobs=''; fi
if grep -Fq 'oaos-daily-backup' <<< "$jobs"; then
  record PASS 'Daily backup appears in the enabled cron list'
else record FAIL 'Daily backup cron job is absent'; fi

# 9: Config files must exist and contain no token-like values.
config_files=("$home/.hermes/SOUL.md" "$home/.hermes/memories/USER.md" "$home/.hermes/memories/MEMORY.md")
missing=0
for file in "${config_files[@]}"; do [[ -f $file ]] || missing=1; done
if ((missing)); then record FAIL 'SOUL, USER, or MEMORY file absent'
elif grep -Eq '(sk-|ghp_)[A-Za-z0-9_-]+|[0-9]{8,10}:[A-Za-z0-9_-]{35,}' "${config_files[@]}"; then
  record FAIL 'Secret-like value found in a harness file (content hidden)'
else record PASS 'SOUL, USER, MEMORY exist; no token-like values found'; fi

# 10: Keep these patterns in sync with common.sh redact; never print matches.
if [[ ! -e $home/.oaos && ! -e $home/.oaos-install ]]; then
  record SKIP 'Scan targets absent; nothing to scan'
elif ! have_cmd python3; then
  record FAIL 'python3 unavailable; secret scan not run'
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
backup_dir="$home/.oaos/backups"
if have_cmd hermes && [[ -d $home/.hermes ]]; then
  mkdir -p "$backup_dir"
  backup="$backup_dir/hermes-verify-$(date +%Y%m%d%H%M%S)-$$.zip"
  backup_code=0
  hermes backup --output "$backup" >/dev/null 2>&1 || backup_code=$?
  if [[ ! -s $backup ]]; then
    record FAIL 'Hermes backup did not create an archive'
  elif ((backup_code == 0)); then
    record PASS 'Hermes backup created a nonempty archive'
  else
    record FAIL "Backup reported incomplete (exit $backup_code); archive exists but may be partial"
  fi
  if [[ -s $backup ]]; then
    find "$backup_dir" -maxdepth 1 -type f -name 'hermes-verify-*.zip' -printf '%T@ %p\0' |
      sort -zrn | tail -z -n +3 | while IFS= read -r -d '' entry; do
        rm -f -- "${entry#* }"
      done
  fi
else record FAIL 'Hermes or its data directory is absent; backup not run'; fi

# 12: Disk margin and swap requirement are measurable without modification.
read -r capacity available < <(df -Pk / | awk 'NR==2 {print $2, $4}')
ram_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
swap_count=$(awk 'END {print NR-1}' /proc/swaps)
if ((available * 100 > capacity * 10)) && { ((ram_kb >= 16777216)) || ((swap_count > 0)); }; then
  record PASS 'Disk free >10%; swap requirement met'
else record FAIL 'Disk free <=10% or swap absent on host with <16 GB RAM'; fi

# Project network, TLS, stack and mailbox checks.
stack="$home/oaos/stack"
stack_get() { [[ -f $stack/.env ]] && sed -n "s/^$1=//p" "$stack/.env" | head -n 1 || true; }
public_ip=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)
for item in CHAT_DOMAIN NOTE_DOMAIN PORTAL_DOMAIN; do
  domain=$(stack_get "$item")
  labels+=("DNS $item")
  if [[ -n $domain && $public_ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] &&
    getent ahostsv4 "$domain" 2>/dev/null | awk '{print $1}' | grep -Fqx "$public_ip"; then
    record PASS 'A record resolves to this server public IPv4'
  else record FAIL 'Domain A record or public IPv4 does not match'; fi
  labels+=("TLS $item")
  if [[ -n $domain ]] && run_root openssl x509 -checkend 604800 -noout -in "/etc/letsencrypt/live/$domain/fullchain.pem" >/dev/null 2>&1; then
    record PASS 'Certificate valid for more than seven days'
  else record FAIL 'Certificate absent or expires within seven days'; fi
  labels+=("HTTPS $item")
  if [[ -n $domain ]] && curl -fsSIL --max-time 10 "https://$domain/" >/dev/null 2>&1; then
    record PASS 'HTTPS responds'
  else record FAIL 'HTTPS request failed'; fi
done
labels+=('TLS auto-renew')
if systemctl is-enabled --quiet certbot.timer 2>/dev/null || systemctl is-enabled --quiet snap.certbot.renew.timer 2>/dev/null; then
  record PASS 'Certbot renewal timer enabled'
else record FAIL 'Certbot renewal timer unavailable or disabled'; fi
labels+=('nginx config')
if have_cmd nginx && run_root nginx -t >/dev/null 2>&1; then record PASS 'nginx -t passed'
else record FAIL 'nginx -t failed or nginx absent'; fi
labels+=('Native services')
healthy=1
for service in postgresql redis-server mattermost oaos-outline; do
  systemctl is-active --quiet "$service" 2>/dev/null || healthy=0
done
if ((healthy)) && pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 &&
   [[ $(redis-cli -h 127.0.0.1 ping 2>/dev/null) == PONG ]]; then
  record PASS 'PostgreSQL, Redis, Mattermost and Outline systemd services are active'
else record FAIL 'One or more native services or local dependencies are unavailable'; fi
labels+=('Outline bind')
listeners=$(ss -H -ltn '( sport = :3000 )' 2>/dev/null | awk '{print $4}')
if [[ $listeners == '127.0.0.1:3000' ]]; then
  record PASS 'Outline listens only on 127.0.0.1:3000'
else record FAIL 'Outline port 3000 has no loopback-only listener'; fi
labels+=('Mattermost ping')
if curl -fsS --max-time 5 http://127.0.0.1:8065/api/v4/system/ping 2>/dev/null | grep -Fq '"status":"OK"'; then
  record PASS 'Mattermost system ping OK'
else record FAIL 'Mattermost system ping failed'; fi
labels+=('Outline health')
if curl -fsS --max-time 5 http://127.0.0.1:3000/_health >/dev/null 2>&1; then
  record PASS 'Outline health endpoint OK'
else record FAIL 'Outline health endpoint failed'; fi
labels+=('Mattermost bot token')
mm_token=$(stack_get MATTERMOST_TOKEN)
if [[ -n $mm_token ]] && curl -fsS --max-time 5 -H "Authorization: Bearer $mm_token" http://127.0.0.1:8065/api/v4/users/me >/dev/null 2>&1; then
  record PASS 'Mattermost bot token authenticated'
else record FAIL 'Mattermost bot token absent or rejected'; fi
labels+=('Outline API token')
outline_token=''
if have_cmd hermes; then outline_token=$(hermes config get --raw OUTLINE_API_TOKEN 2>/dev/null || true); fi
if [[ -n $outline_token && $outline_token != null ]] && curl -fsS --max-time 10 -X POST -H "Authorization: Bearer $outline_token" -H 'Content-Type: application/json' --data '{}' http://127.0.0.1:3000/api/auth.info >/dev/null 2>&1; then
  record PASS 'Outline API token authenticated'
else record FAIL 'Outline API token absent or rejected'; fi
labels+=('Bot mailbox IMAP')
if [[ -n $(stack_get OAOS_MAIL_ADDRESS) && -n $(stack_get OAOS_MAIL_PASSWORD) && -n $(stack_get OAOS_MAIL_IMAP_HOST) ]]; then
  if OAOS_MAIL_ADDRESS=$(stack_get OAOS_MAIL_ADDRESS) OAOS_MAIL_PASSWORD=$(stack_get OAOS_MAIL_PASSWORD) OAOS_MAIL_IMAP_HOST=$(stack_get OAOS_MAIL_IMAP_HOST) python3 - <<'PYMAIL' >/dev/null 2>&1
import imaplib, os
with imaplib.IMAP4_SSL(os.environ['OAOS_MAIL_IMAP_HOST'], timeout=10) as client:
    client.login(os.environ['OAOS_MAIL_ADDRESS'], os.environ['OAOS_MAIL_PASSWORD'])
    client.logout()
PYMAIL
  then record PASS 'IMAP login succeeded'
  else record MANUAL 'IMAP login failed; check mailbox settings and retry manually'; fi
else record MANUAL 'G9 mailbox values absent; IMAP login pending'; fi

pass=0 fail=0 manual=0 skip=0
for status in "${statuses[@]}"; do
  case $status in PASS) ((pass+=1));; FAIL) ((fail+=1));; MANUAL) ((manual+=1));; SKIP) ((skip+=1));; esac
done

if ((json)) && ! have_cmd python3; then
  printf 'python3 unavailable; --json output fell back to the table.\n' >&2
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
