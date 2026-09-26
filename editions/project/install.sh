#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"

stages=(prep hermes llm telegram wiki harness cron stack ingress mail team gateway verify)
export OAOS_STATE_EDITION=project
export OAOS_STAGES="${stages[*]}"
selected=()
OAOS_DRY_RUN=0
yes=0
skip_verify=0
show_status=0
timezone=''

usage() {
  cat <<'EOF'
Usage: bash editions/project/install.sh [--dry-run] [--stage name[,name...]] [--status] [--yes] [--timezone Area/City] [--skip-verify] [--help]

Stages: prep, hermes, llm, telegram, wiki, harness, cron, stack, ingress, mail, team, gateway, verify.
The default runs all stages in order. Completed stages are skipped on rerun.
--dry-run prints a plan without writing state or changing the system.
--yes approves running a downloaded official Hermes installer.
Exit codes: 0 = selected stages complete; 3 = one or more blocked; 1 = error.
EOF
}

valid_stage() { local s; for s in "${stages[@]}"; do [[ $s == "$1" ]] && return 0; done; return 1; }
while (($#)); do
  case $1 in
    --dry-run) OAOS_DRY_RUN=1; shift ;;
    --status) show_status=1; shift ;;
    --yes) yes=1; shift ;;
    --skip-verify) skip_verify=1; shift ;;
    --stage)
      (($# >= 2)) || { usage >&2; exit 1; }
      [[ $2 =~ ^[a-z]+(,[a-z]+)*$ ]] || die 'Invalid --stage value; use comma-separated stage names.'
      IFS=, read -r -a requested <<< "$2"
      for s in "${requested[@]}"; do valid_stage "$s" || die "Unknown stage: $s"; selected+=("$s"); done
      shift 2 ;;
    --timezone)
      (($# >= 2)) || { usage >&2; exit 1; }
      timezone=$2; [[ $timezone =~ ^[A-Za-z_]+(/[A-Za-z0-9_+-]+)+$ ]] || die 'Invalid timezone name.'
      shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
done

[[ -n ${HOME:-} ]] || { printf 'HOME is not set.\n' >&2; exit 1; }
if ((OAOS_DRY_RUN)); then export OAOS_NO_LOG_FILE=1; fi

show_table() {
  local s
  printf '%-12s %-10s\n' STAGE STATUS
  for s in "${stages[@]}"; do printf '%-12s %-10s\n' "$s" "$(stage_status "$s")"; done
}

if ((show_status)); then
  if ! state_valid; then printf 'Install state file is invalid JSON: %s\n' "$(oaos_state_file)" >&2; exit 1; fi
  show_table
  exit 0
fi
if (( ! OAOS_DRY_RUN )) && ! state_valid; then
  corrupt="$(oaos_state_file).corrupt-$(date +%s)"
  [[ ! -e $corrupt ]] || die 'Corrupt state backup already exists; retry later.'
  mv -- "$(oaos_state_file)" "$corrupt" || die 'Failed to preserve corrupt state file.'
  warn "Invalid state file preserved at $corrupt; starting fresh."
fi
info 'Project installation started.'
check_os

selected_stage() {
  local item
  ((${#selected[@]} == 0)) && return 0
  for item in "${selected[@]}"; do [[ $item == "$1" ]] && return 0; done
  return 1
}

need_hermes() { have_cmd hermes || { warn 'Hermes is unavailable; complete the hermes stage first.'; return 3; }; }
config_value() { hermes config get --raw "$1" 2>/dev/null || true; }

do_prep() {
  local blocked=0 pkg ram_kb
  local missing=()
  mkdir -p "$(oaos_home)/.oaos/logs" "$(oaos_home)/.oaos/backups" "$(oaos_home)/.oaos-install"
  for pkg in git curl xz-utils ca-certificates nginx certbot python3-certbot-nginx ufw rsync iproute2 openssl; do
    if ! have_cmd dpkg-query || ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then missing+=("$pkg"); fi
  done
  if ((${#missing[@]})); then
    if have_cmd apt-get; then
      run_root apt-get update && run_root apt-get install -y "${missing[@]}" || blocked=1
    else warn 'Package manager unavailable; required packages need manual installation.'; blocked=1; fi
  fi
  if have_cmd ufw; then
    for pkg in OpenSSH 80/tcp 443/tcp; do run_root ufw allow "$pkg" >/dev/null || blocked=1; done
  fi
  ram_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
  if ((ram_kb < 16777216)) && [[ $(awk 'END {print NR-1}' /proc/swaps) == 0 ]]; then
    if [[ -e /swapfile ]]; then
      warn 'An inactive /swapfile exists; preserving it for manual review.'; blocked=1
    else
      run_root fallocate -l 8G /swapfile && run_root chmod 600 /swapfile && run_root mkswap /swapfile && run_root swapon /swapfile || blocked=1
      if ((blocked == 0)); then
        run_root bash -c 'grep -qE "^/swapfile[[:space:]]" /etc/fstab || printf "%s\n" "/swapfile none swap sw 0 0" >> /etc/fstab' || blocked=1
      fi
    fi
  fi
  if [[ -n $timezone ]]; then
    if [[ $(timedatectl show -p Timezone --value 2>/dev/null || true) != "$timezone" ]]; then
      run_root timedatectl set-timezone "$timezone" || blocked=1
    fi
  fi
  if [[ ! -f /etc/systemd/logind.conf.d/oaos-project.conf ]]; then
    if run_root install -d -m 755 /etc/systemd/logind.conf.d; then
      printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nIdleAction=ignore\n' |
        run_root tee /etc/systemd/logind.conf.d/oaos-project.conf >/dev/null || blocked=1
      warn 'The logind drop-in takes effect after a safe logind restart or reboot.'
    else blocked=1; fi
  fi
  run_root systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target || blocked=1
  ((blocked == 0)) || return 3
}

do_hermes() {
  local installer cache answer actual_sha
  if have_cmd hermes && hermes --version >/dev/null 2>&1; then return 0; fi
  have_cmd curl || { warn 'curl is required to download Hermes.'; return 3; }
  cache="$(oaos_home)/.oaos/cache"
  mkdir -p "$cache"
  installer="$cache/hermes-install.sh"
  if [[ ! -e $installer ]]; then
    curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 10 --retry 2 'https://hermes-agent.nousresearch.com/install.sh' -o "$installer" || { rm -f -- "$installer"; warn 'Hermes installer download failed.'; return 1; }
  fi
  if [[ ! -s $installer ]]; then rm -f -- "$installer"; warn 'Hermes installer is empty.'; return 1; fi
  bash -n "$installer" || { rm -f -- "$installer"; warn 'Downloaded installer failed syntax validation.'; return 1; }
  actual_sha=$(sha256sum "$installer" | awk '{print $1}') || return 1
  printf '%s  %s\n' "$actual_sha" "$installer"
  if [[ -n ${OAOS_HERMES_INSTALLER_SHA256:-} && $actual_sha != "$OAOS_HERMES_INSTALLER_SHA256" ]]; then
    rm -f -- "$installer"
    warn 'Hermes installer checksum mismatch.'
    return 1
  fi
  if ((yes == 0)); then
    if [[ -t 0 ]]; then
      printf 'Run the saved Hermes installer? [y/N] ' >&2
      read -r answer || answer=''
      [[ $answer == y || $answer == Y ]] || { warn 'Hermes installer awaits approval (--yes).'; return 3; }
    else warn 'Hermes installer awaits approval (--yes).'; return 3; fi
  fi
  bash "$installer" || return 1
  hash -r
  if ! have_cmd hermes || ! hermes --version >/dev/null 2>&1; then
    warn 'Hermes is not on PATH after installation; open a new shell.'
    return 3
  fi
}

do_llm() {
  local key value model
  need_hermes || return 3
  model=$(config_value model)
  for key in NOUS_API_KEY OPENROUTER_API_KEY OPENCODE_API_KEY VERCEL_AI_GATEWAY_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY; do
    value=$(config_value "$key")
    if [[ -n $value && $value != 'null' ]]; then
      if [[ -n $model && $model != 'null' ]]; then return 0; fi
    fi
  done
  warn 'G1: Choose a provider at https://opencode.ai/go or https://hermes-agent.nousresearch.com/docs. Store its key with hermes config set <PROVIDER>_API_KEY, then select a model with hermes model. Never paste a key into logs.'
  return 3
}

do_telegram() {
  local token users result
  need_hermes || return 3
  token=$(config_value TELEGRAM_BOT_TOKEN)
  users=$(config_value TELEGRAM_ALLOWED_USERS)
  if [[ -z $token || -z $users || $token == null || $users == null ]]; then
    warn 'G2/G3: Create a bot at https://t.me/BotFather and get your numeric ID at https://t.me/userinfobot. Store both using hermes config set TELEGRAM_BOT_TOKEN and hermes config set TELEGRAM_ALLOWED_USERS.'
    return 3
  fi
  result=$(printf '%s' "$token" | oaos_python -c '
import json, sys, urllib.error, urllib.request
token = sys.stdin.read().strip()
try:
    with urllib.request.urlopen("https://api.telegram.org/bot" + token + "/getMe", timeout=10) as response:
        data = json.load(response)
    print("valid" if data.get("ok") is True else "rejected")
except urllib.error.HTTPError as error:
    print("HTTP " + str(error.code))
except Exception:
    print("network error")
')
  if [[ $result != valid ]]; then
    warn "Telegram getMe failed ($result). Check the bot token or network and retry; https://t.me/BotFather"
    return 3
  fi
}

do_gateway() {
  need_hermes || return 3
  if ! hermes gateway status 2>/dev/null | grep -Eq 'Gateway is running'; then
    hermes gateway install --start-on-login --start-now >/dev/null || return 1
  fi
  if ! systemctl --user is-enabled hermes-gateway >/dev/null 2>&1; then
    warn 'Gateway user service is not enabled.'; return 3
  fi
  if [[ $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true) != yes ]]; then
    run_root loginctl enable-linger "$(id -un)" || return 3
  fi
  hermes gateway status 2>/dev/null | grep -Eq 'Gateway is running' || return 3
}

do_wiki() {
  local wiki
  wiki="$(oaos_home)/data/wiki"
  if [[ -e $wiki ]]; then
    [[ -d $wiki/.git ]] || { warn 'Wiki path already exists but is not a git repository; preserving it.'; return 3; }
    git -C "$wiki" log -1 --format=%h >/dev/null 2>&1 || { warn 'Existing wiki has no commit; preserving it.'; return 3; }
    return 0
  fi
  mkdir -p "$(dirname "$wiki")"
  git init -q -b main "$wiki" || return 1
  cat > "$wiki/index.md" <<'EOF'
# Project Wiki

Purpose: keep team knowledge in a local, versioned workspace.

## Areas

- Inbox
- Projects
- People
- Resources
- Archive
EOF
  printf '# Project Wiki\n\nStart at [index.md](index.md). Keep private information out of public remotes.\n' > "$wiki/README.md"
  printf '.DS_Store\n*.swp\n' > "$wiki/.gitignore"
  git -C "$wiki" add index.md README.md .gitignore
  git -C "$wiki" -c user.name='OAOS Bootstrap' -c user.email='bootstrap@localhost' commit -qm 'wiki: seed project index' || return 1
}

do_harness() {
  local home name target
  home="$(oaos_home)/.hermes"
  mkdir -p "$home/memories"
  for name in SOUL USER MEMORY; do
    if [[ $name == SOUL ]]; then target="$home/SOUL.md"; else target="$home/memories/$name.md"; fi
    if [[ ! -e $target ]]; then
      { printf '<!-- Draft — refine through conversation. Do not store secrets. -->\n'; cat "$repo_root/harness/templates/$name.md"; } > "$target" || return 1
    fi
  done
}

do_cron() {
  local scripts jobs name=oaos-daily-backup script
  scripts="$(oaos_home)/.hermes/scripts"
  need_hermes || return 3
  mkdir -p "$scripts"
  jobs=$(hermes cron list --all 2>/dev/null) || return 3
  if ! grep -Fq "$name" <<< "$jobs"; then
    script="$scripts/$name.sh"
    if [[ -e $script ]]; then warn "Existing script for $name needs review; preserving it."; return 3; fi
    cat > "$script" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
mkdir -p "$HOME/.oaos/backups"
target="$HOME/.oaos/backups/oaos-$(date +%Y%m%d-%H%M%S).tar.gz"
tmp=$(mktemp "$target.tmp.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
tar -czf "$tmp" -C "$HOME" .hermes data/wiki
mv -- "$tmp" "$target"
ls -1t "$HOME/.oaos/backups"/oaos-*.tar.gz 2>/dev/null | tail -n +8 | while IFS= read -r old; do rm -f -- "$old"; done
EOF
    chmod 700 "$script"
    hermes cron create --name "$name" --script "$script" --no-agent --deliver local '0 3 * * *' >/dev/null || return 1
  fi
  jobs=$(hermes cron list --all 2>/dev/null) || return 3
  grep -Fq "$name" <<< "$jobs" || return 3
}

# Values in the stack environment are simple one-line literals; never source this file.
stack_dir() { printf '%s/oaos/stack' "$(oaos_home)"; }
stack_get() { [[ -f $(stack_dir)/.env ]] && sed -n "s/^$1=//p" "$(stack_dir)/.env" | head -n 1 || true; }
stack_put() {
  local key=$1 value=$2 file
  file="$(stack_dir)/.env"
  [[ $key =~ ^[A-Z_]+$ && $value =~ ^[A-Za-z0-9@._/+:=-]+$ ]] || return 1
  OAOS_ENV_VALUE=$value oaos_python - "$file" "$key" <<'PY'
import os, pathlib, sys
path, key = pathlib.Path(sys.argv[1]), sys.argv[2]
lines = path.read_text().splitlines() if path.exists() else []
lines = [line for line in lines if not line.startswith(key + '=')]
lines.append(key + '=' + os.environ['OAOS_ENV_VALUE'])
import tempfile
with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.env.tmp-', delete=False) as tmp:
    os.chmod(tmp.name, 0o600)
    tmp.write('\n'.join(lines) + '\n')
    temp_name = tmp.name
os.replace(temp_name, path)
PY
}
stack_remove() {
  local key=$1 file
  file="$(stack_dir)/.env"
  [[ $key =~ ^[A-Z_]+$ ]] || return 1
  [[ -f $file ]] || return 0
  grep -q "^$key=" "$file" || return 0
  oaos_python - "$file" "$key" <<'PY'
import os, pathlib, sys, tempfile
path, key = pathlib.Path(sys.argv[1]), sys.argv[2]
lines = [line for line in path.read_text().splitlines() if not line.startswith(key + '=')]
with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.env.tmp-', delete=False) as tmp:
    os.chmod(tmp.name, 0o600)
    tmp.write('\n'.join(lines) + '\n')
    temp_name = tmp.name
os.replace(temp_name, path)
PY
}
project_domain() {
  local key=$1 value
  value=${!key:-}
  if [[ -z $value && -n ${OAOS_BASE_DOMAIN:-} ]]; then
    case $key in OAOS_CHAT_DOMAIN) value="chat.$OAOS_BASE_DOMAIN";; OAOS_NOTE_DOMAIN) value="note.$OAOS_BASE_DOMAIN";; OAOS_PORTAL_DOMAIN) value="portal.$OAOS_BASE_DOMAIN";; esac
  fi
  [[ $value =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,}$ ]] || return 3
  printf '%s' "$value"
}
# shellcheck source=editions/project/native-stack.sh
. "$repo_root/editions/project/native-stack.sh"

public_ip() { curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true; }
dns_ok() {
  local host=$1 ip=$2
  getent ahostsv4 "$host" 2>/dev/null | awk '{print $1}' | grep -Fqx "$ip"
}
do_ingress() {
  local ip key domain template target root=/var/www/oaos-portal email
  if ! have_cmd nginx || ! have_cmd certbot; then warn 'nginx and certbot nginx plugin required; run prep.'; return 3; fi
  email=${OAOS_ACME_EMAIL:-}
  [[ $email =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || { warn 'G6: set OAOS_ACME_EMAIL for certificate registration.'; return 3; }
  ip=$(public_ip)
  [[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { warn 'Public IPv4 lookup failed; check network and retry.'; return 3; }
  for key in CHAT_DOMAIN NOTE_DOMAIN PORTAL_DOMAIN; do
    domain=$(stack_get "$key")
    if [[ -z $domain ]]; then
      warn "G6: $key is missing from the stack environment; run stack with the domain values first."; return 3
    fi
    if ! dns_ok "$domain" "$ip"; then
      warn "G6: $key A record must resolve to this server public IPv4 ($ip)."; return 3
    fi
  done
  run_root install -d -m 755 "$root" /etc/nginx/sites-available /etc/nginx/sites-enabled || return 3
  if ! run_root test -e "$root/index.html"; then
    printf '<!doctype html><html lang="ko"><meta charset="utf-8"><title>OAOS Project</title><h1>OAOS Project</h1><p><a href="https://%s">Mattermost</a> · <a href="https://%s">Outline</a></p></html>\n' "$(stack_get CHAT_DOMAIN)" "$(stack_get NOTE_DOMAIN)" |
      run_root tee "$root/index.html" >/dev/null || return 3
  fi
  for key in chat note portal; do
    case $key in chat) domain=$(stack_get CHAT_DOMAIN);; note) domain=$(stack_get NOTE_DOMAIN);; portal) domain=$(stack_get PORTAL_DOMAIN);; esac
    template="$repo_root/editions/project/ingress/$key.conf"
    target="/etc/nginx/sites-available/oaos-$key.conf"
    if run_root test -e "$target"; then
      if ! run_root grep -Fq '# OAOS Project managed vhost' "$target"; then
        warn "Existing nginx vhost $target is not managed by OAOS; preserving it."; return 3
      fi
    else
      sed -e "s/__CHAT_DOMAIN__/$(stack_get CHAT_DOMAIN)/g" -e "s/__NOTE_DOMAIN__/$(stack_get NOTE_DOMAIN)/g" -e "s/__PORTAL_DOMAIN__/$(stack_get PORTAL_DOMAIN)/g" -e "s@__PORTAL_ROOT__@$root@g" "$template" |
        run_root tee "$target" >/dev/null || return 3
    fi
    run_root ln -sfn "$target" "/etc/nginx/sites-enabled/oaos-$key.conf" || return 3
  done
  run_root nginx -t >/dev/null || return 3
  run_root systemctl reload nginx || return 3
  for key in CHAT_DOMAIN NOTE_DOMAIN PORTAL_DOMAIN; do
    domain=$(stack_get "$key")
    if ! run_root openssl x509 -checkend 2592000 -noout -in "/etc/letsencrypt/live/$domain/fullchain.pem" >/dev/null 2>&1; then
      run_root certbot --nginx --non-interactive --agree-tos --email "$email" -d "$domain" --redirect >/dev/null || { warn "TLS issuance failed for $domain; check DNS, port 80, and certbot logs."; return 3; }
    fi
  done
  run_root nginx -t >/dev/null || return 3
  if ! systemctl is-enabled --quiet certbot.timer 2>/dev/null && ! systemctl is-enabled --quiet snap.certbot.renew.timer 2>/dev/null; then
    warn 'Certbot auto-renew timer is not enabled.'; return 3
  fi
}

do_mail() {
  local address password imap smtp dir
  address=${OAOS_MAIL_ADDRESS:-$(stack_get OAOS_MAIL_ADDRESS)}
  password=${OAOS_MAIL_PASSWORD:-$(stack_get OAOS_MAIL_PASSWORD)}
  imap=${OAOS_MAIL_IMAP_HOST:-$(stack_get OAOS_MAIL_IMAP_HOST)}
  smtp=${OAOS_MAIL_SMTP_HOST:-$(stack_get OAOS_MAIL_SMTP_HOST)}
  [[ -n $address && -n $password && -n $imap && -n $smtp ]] || {
    warn 'G9: set OAOS_MAIL_ADDRESS, OAOS_MAIL_PASSWORD, OAOS_MAIL_IMAP_HOST, OAOS_MAIL_SMTP_HOST in the environment and rerun mail.'; return 3; }
  [[ $address =~ ^[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || {
    warn 'G9: mail address contains characters unsupported by the stack environment; use a standard address with letters, digits, dot, underscore, plus, or hyphen.'; return 3; }
  [[ $imap =~ ^[A-Za-z0-9.-]+$ && $smtp =~ ^[A-Za-z0-9.-]+$ ]] || { warn 'G9: invalid IMAP or SMTP host name.'; return 3; }
  [[ $password =~ ^[A-Za-z0-9_-]+$ ]] || { warn 'Mail password must be a one-line app password without spaces; remove presentation spaces.'; return 3; }
  have_cmd himalaya || { warn 'Install Himalaya from https://pimalaya.org/himalaya/ after verifying its release integrity, then rerun mail.'; return 3; }
  [[ $(himalaya --version 2>/dev/null) =~ [[:space:]]1\. ]] || { warn 'Himalaya configuration requires a verified 1.x CLI; configure this version manually.'; return 3; }
  stack_put OAOS_MAIL_ADDRESS "$address" && stack_put OAOS_MAIL_PASSWORD "$password" &&
    stack_put OAOS_MAIL_IMAP_HOST "$imap" && stack_put OAOS_MAIL_SMTP_HOST "$smtp" || return 1
  dir="$(oaos_home)/.config/himalaya"
  mkdir -p "$dir"
  chmod 700 "$dir"
  if [[ -e $dir/config.toml ]]; then
    warn 'Existing Himalaya config preserved; verify the oaos-bot account manually.'
  else
    cat > "$dir/config.toml" <<EOF2
[accounts.oaos-bot]
default = true
email = "$address"
display-name = "OAOS Bot"
backend.type = "imap"
backend.host = "$imap"
backend.port = 993
backend.encryption.type = "tls"
backend.login = "$address"
backend.auth.type = "password"
backend.auth.cmd = "sed -n 's/^OAOS_MAIL_PASSWORD=//p' $(stack_dir)/.env"
message.send.backend.type = "smtp"
message.send.backend.host = "$smtp"
message.send.backend.port = 587
message.send.backend.encryption.type = "start-tls"
message.send.backend.login = "$address"
message.send.backend.auth.type = "password"
message.send.backend.auth.cmd = "sed -n 's/^OAOS_MAIL_PASSWORD=//p' $(stack_dir)/.env"
EOF2
    chmod 600 "$dir/config.toml"
  fi
  if ! timeout 20s himalaya envelope list -a oaos-bot >/dev/null 2>&1; then
    warn 'G9: Himalaya IMAP login failed; review mailbox host, app password, and config.'; return 3
  fi
  native_outline_config || { warn 'Outline mail configuration failed.'; return 3; }
  if [[ $native_outline_changed == changed ]]; then
    run_root systemctl restart oaos-outline || { warn 'Outline mail configuration reload failed.'; return 3; }
  fi
  warn 'G9: confirm one real send/receive flow through the agent.'
}

do_team() {
  local chat note output token outline_token admin=0
  need_hermes || return 3
  chat=$(stack_get CHAT_DOMAIN)
  [[ -n $chat ]] || { warn 'Stack domain absent; run stack first.'; return 3; }
  note=$(stack_get NOTE_DOMAIN)
  [[ -x /opt/mattermost/bin/mmctl ]] || { warn 'Mattermost mmctl is absent from /opt/mattermost/bin.'; return 3; }
  output=$(run_root runuser -u mattermost -- /opt/mattermost/bin/mmctl --local user list --role system_admin --json 2>/dev/null) || output=''
  if [[ -z $output ]]; then
    warn 'mmctl local mode unavailable; confirm the administrator and create the Hermes bot/token in Mattermost System Console manually.'
  else
    if printf '%s' "$output" | oaos_python -c 'import json,sys; x=json.load(sys.stdin); sys.exit(0 if len(x)>0 else 1)' 2>/dev/null; then admin=1; fi
    ((admin)) || { warn "G7: create the first Mattermost administrator at https://$chat, then rerun team."; return 3; }
    run_root runuser -u mattermost -- /opt/mattermost/bin/mmctl --local config set ServiceSettings.SiteURL "https://$chat" >/dev/null 2>&1 || warn 'mmctl SiteURL update unavailable; review Mattermost console.'
  fi
  token=$(stack_get MATTERMOST_TOKEN)
  [[ -n $token ]] || {
    warn 'G7: create the Hermes bot and token in Mattermost System Console, store MATTERMOST_TOKEN in stack/.env, then rerun team.'; return 3;
  }
  if [[ -n $token ]]; then
    if ! curl -fsS --max-time 5 -H "Authorization: Bearer $token" http://127.0.0.1:8065/api/v4/users/me >/dev/null 2>&1; then
      warn 'G7: stored Mattermost bot token was rejected; replace it before retrying.'; return 3
    fi
    need_hermes || return 3
    hermes config set MATTERMOST_URL "https://$chat" >/dev/null 2>&1 || return 3
    hermes config set MATTERMOST_TOKEN "$token" >/dev/null 2>&1 || return 3
  fi
  cat > "$(oaos_home)/oaos/team-onboarding.md" <<EOF2
# OAOS Project 팀 온보딩

1. Mattermost 관리자: https://$chat 에서 팀을 만들고 멤버를 초대합니다.
2. Hermes 봇을 팀에 추가하고 MATTERMOST_ALLOWED_USERS에 허용할 계정을 설정합니다.
3. Outline 관리자 가입 후 G8 API 토큰을 발급받고 연결을 검증합니다.
4. 허용 계정과 비허용 계정의 채팅 동작을 각각 확인합니다.
5. 봇 메일함의 송신·수신을 확인합니다.
EOF2
  if [[ -n ${OAOS_MATTERMOST_ALLOWED_USERS:-} && -n $token ]]; then
    hermes config set MATTERMOST_ALLOWED_USERS "$OAOS_MATTERMOST_ALLOWED_USERS" >/dev/null 2>&1 || return 3
  fi
  outline_token=${OAOS_OUTLINE_API_TOKEN:-$(config_value OUTLINE_API_TOKEN)}
  [[ -n $outline_token && $outline_token != null ]] || outline_token=$(stack_get OUTLINE_API_TOKEN)
  [[ -n $outline_token ]] || { warn "G8: create an Outline admin at https://$note and provide OAOS_OUTLINE_API_TOKEN."; return 3; }
  if curl -fsS --max-time 10 -X POST -H "Authorization: Bearer $outline_token" -H 'Content-Type: application/json' --data '{}' "http://127.0.0.1:3000/api/auth.info" >/dev/null 2>&1; then
    hermes config set OUTLINE_API_TOKEN "$outline_token" >/dev/null 2>&1 || return 3
    stack_remove OUTLINE_API_TOKEN || return 1
  else warn 'G8: Outline API token validation failed; issue a new token and retry.'; return 3; fi
}

do_verify() {
  local output code=0 manual
  # Preserve the installer user's HOME and Hermes context; the verifier elevates privileged probes.
  output=$(bash "$repo_root/bootstrap/verify/project-verify.sh") || code=$?
  printf '%s\n' "$output"
  if ((code == 0)); then
    manual=$(sed -nE 's/^Summary: PASS=[0-9]+ FAIL=[0-9]+ MANUAL=([0-9]+) SKIP=[0-9]+$/\1/p' <<< "$output" | tail -n 1)
    [[ $manual =~ ^[0-9]+$ ]] || { warn 'Verify summary is missing; review the verification output.'; return 1; }
    stage_detail="Completed; MANUAL=$manual"
    info "Verify automated checks passed; MANUAL=$manual item(s) require human evidence."
  fi
  return "$code"
}

# Stage dispatch is deliberately explicit: no user-controlled command name is evaluated.
run_stage() {
  case $1 in
    prep) do_prep ;; hermes) do_hermes ;; llm) do_llm ;;
    telegram) do_telegram ;; wiki) do_wiki ;;
    harness) do_harness ;; cron) do_cron ;; stack) do_stack ;;
    ingress) do_ingress ;; mail) do_mail ;; team) do_team ;;
    gateway) do_gateway ;; verify) do_verify ;;
  esac
}

result=0
for stage in "${stages[@]}"; do
  selected_stage "$stage" || continue
  if [[ $stage == verify ]] && ((skip_verify)); then info 'Skipping verify by request.'; continue; fi
  if stage_done "$stage"; then info "Skipping $stage (done)."; continue; fi
  if ((OAOS_DRY_RUN)); then info "Would run $stage (current: $(stage_status "$stage"))."; continue; fi
  info "Running $stage."
  code=0
  stage_detail=Completed
  run_stage "$stage" || code=$?
  case $code in
    0) stage_mark "$stage" 'done' "$stage_detail"; info "$stage done." ;;
    3) stage_mark "$stage" blocked 'Needs a gate, prerequisite, or permission'; warn "$stage blocked."; ((result == 1)) || result=3 ;;
    *) stage_mark "$stage" failed 'Command failed; review the install log'; warn "$stage failed."; result=1 ;;
  esac
done
show_table
exit "$result"
