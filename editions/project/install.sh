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

stages=(prep hermes llm telegram wiki harness cron stack ingress mail team gateway verify)
export OAOS_STATE_EDITION=project
export OAOS_STAGES="${stages[*]}"
selected=()
OAOS_DRY_RUN=0
yes=0
skip_verify=0
show_status=0
recheck=0
timezone=''

usage() {
  cat <<'EOF'
Usage: bash editions/project/install.sh [--dry-run] [--stage name[,name...]] [--status] [--yes]
                                        [--recheck] [--reverify] [--timezone Area/City] [--skip-verify] [--help]

Stages: prep, hermes, llm, telegram, wiki, harness, cron, stack, ingress, mail, team, gateway, verify.
The default runs all stages in order. Completed stages are skipped on rerun,
except verify, which always runs again.
--recheck   re-run selected stages even when recorded as done (each is idempotent).
--reverify  run only the verify stage (same as --stage verify).
--dry-run   prints a plan without writing state or changing the system.
--yes       approves running the downloaded official Hermes installer.
Gate values are stored with: bash bootstrap/bin/oaos-secret-set KEY (hidden prompt; never argv).
Exit codes: 0 = selected stages complete and verify all PASS;
            2 = no failure or block, but MANUAL verify items are pending;
            3 = one or more stages blocked; 1 = error or a verify FAIL.
EOF
}

valid_stage() { local s; for s in "${stages[@]}"; do [[ $s == "$1" ]] && return 0; done; return 1; }
while (($#)); do
  case $1 in
    --dry-run) OAOS_DRY_RUN=1; shift ;;
    --status) show_status=1; shift ;;
    --yes) yes=1; shift ;;
    --skip-verify) skip_verify=1; shift ;;
    --recheck) recheck=1; shift ;;
    --reverify) selected+=(verify); shift ;;
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
os_detect >/dev/null || exit 3
if [[ $OAOS_PLATFORM != linux ]]; then
  printf 'BLOCKED: the Project edition runs on an Ubuntu LTS server only.\n' >&2
  exit 3
fi

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
oaos_repo_owner_check "$repo_root" || exit 3
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

# Smoke tests may relocate system paths, only together with OAOS_TEST_MODE=1.
test_root() { if [[ ${OAOS_TEST_MODE:-0} == 1 ]]; then printf '%s' "${OAOS_TEST_SYSTEM_ROOT:-}"; fi; }

need_hermes() { have_cmd hermes || { warn 'Hermes is unavailable; complete the hermes stage first.'; return 3; }; }
config_value() { hermes config get --raw "$1" 2>/dev/null || true; }
secret_hint() { printf 'bash bootstrap/bin/oaos-secret-set %s' "$1"; }
private_dir() { mkdir -p "$1" && chmod 700 "$1"; }

# Values in the stack environment are simple one-line literals; never source this file.
# ~/oaos/stack/.env (600) keeps only service-internal secrets (database passwords, Outline
# keys) and the domains. Gate values and tokens live in the Hermes .env.
stack_dir() { printf '%s/oaos/stack' "$(oaos_home)"; }
stack_get() {
  local file value
  file="$(stack_dir)/.env"
  [[ -f $file ]] || return 0
  value=$(sed -n "s/^$1=//p" "$file")
  printf '%s' "${value%%$'\n'*}"
}
stack_put() {
  local key=$1 value=$2 file
  file="$(stack_dir)/.env"
  [[ $key =~ ^[A-Z_]+$ && $value =~ ^[A-Za-z0-9@._/+:=-]+$ ]] || return 1
  OAOS_ENV_VALUE=$value oaos_python - "$file" "$key" <<'PY'
import os, pathlib, sys, tempfile
path, key = pathlib.Path(sys.argv[1]), sys.argv[2]
lines = path.read_text().splitlines() if path.exists() else []
lines = [line for line in lines if not line.startswith(key + '=')]
lines.append(key + '=' + os.environ['OAOS_ENV_VALUE'])
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
# Earlier releases kept gate secrets in ~/oaos/stack/.env. Move each into the Hermes .env
# (stdin, never argv) unless it is already there, then drop the stack copy.
migrate_stack_secret() {
  local key=$1 value
  value=$(stack_get "$key")
  [[ -n $value ]] || return 0
  if ! oaos_secret_present "$key"; then
    printf '%s' "$value" | oaos_secret_set "$key" 2>/dev/null || { value=''; warn "Could not move $key into the Hermes .env."; return 3; }
    info "Moved $key from the stack .env into the Hermes .env."
  fi
  value=''
  stack_remove "$key" || return 1
}

project_domain() {
  local key=$1 value='' base
  value=$(project_gate_value "$key" || true)
  if [[ -z $value ]] && base=$(project_gate_value OAOS_BASE_DOMAIN); then
    case $key in OAOS_CHAT_DOMAIN) value="chat.$base";; OAOS_NOTE_DOMAIN) value="note.$base";; OAOS_PORTAL_DOMAIN) value="portal.$base";; esac
  fi
  [[ $value =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,}$ ]] || return 3
  printf '%s' "$value"
}
# shellcheck source=editions/project/native-stack.sh
. "$repo_root/editions/project/native-stack.sh"

# --- G0: host capacity, ports, SSH and firewall -------------------------------------------

host_capacity() {
  local level summary below short
  IFS='|' read -r level summary below short < <(project_capacity) || { warn 'G0: host capacity could not be read.'; return 3; }
  case $level in
    ok) info "G0: host capacity $summary meets the 4 vCPU / 16 GB / 200 GB recommendation." ;;
    warn) warn "G0 WARN: $summary is below the recommended 4 vCPU / 16 GB / 200 GB ($below). The Outline build and five services may be slow." ;;
    *)
      if [[ ${OAOS_ALLOW_SMALL_HOST:-0} != 1 ]]; then
        warn "G0 BLOCKED: $summary is below the Project minimum of 2 vCPU / 8 GB / 60 GB ($short). Use a larger VPS, or set OAOS_ALLOW_SMALL_HOST=1 to continue knowingly."
        return 3
      fi
      warn "G0: $summary is below the Project minimum ($short); continuing because OAOS_ALLOW_SMALL_HOST=1." ;;
  esac
}

# A port already in use by something other than its Project owner blocks before stack.
port_owner_installed() {
  case $1 in
    oaos-outline) [[ -e /etc/systemd/system/oaos-outline.service ]] ;;
    *) native_installed "$1" ;;
  esac
}
port_precheck() {
  local entry port owner listeners code conflicts=()
  for entry in $project_ports; do
    port=${entry%%:*}; owner=${entry#*:}
    code=0
    listeners=$(platform_listeners "$port") || code=$?
    if ((code != 0)); then warn 'G0: listener check unavailable (ss missing); install iproute2.'; return 3; fi
    [[ -n $listeners ]] || continue
    port_owner_installed "$owner" || conflicts+=("$port (expected $owner)")
  done
  if ((${#conflicts[@]})); then
    warn "G0 BLOCKED: port(s) already in use by another program: ${conflicts[*]}. Stop or move that service, then rerun prep."
    return 3
  fi
}

ssh_ports() {
  local ports=(22) port
  while IFS= read -r port; do [[ $port =~ ^[0-9]+$ ]] && ports+=("$port"); done < <(run_root sshd -T 2>/dev/null | awk '$1 == "port" {print $2}' || true)
  if [[ -n ${SSH_CONNECTION:-} ]]; then
    port=$(awk '{print $4}' <<< "$SSH_CONNECTION")
    [[ $port =~ ^[0-9]+$ ]] && ports+=("$port")
  fi
  printf '%s\n' "${ports[@]}" | sort -un
}
ssh_key_report() {
  local keys="$HOME/.ssh/authorized_keys" settings
  if [[ -s $keys ]] && grep -Eq '^(ssh-|ecdsa-|sk-)' "$keys"; then
    info 'G0: SSH key login is configured for this account (authorized_keys present).'
  else
    warn "G0 WARN: no SSH public key in $keys. Add one (ssh-copy-id) before relying on key login."
  fi
  settings=$(run_root sshd -T 2>/dev/null || true)
  if grep -Eqx 'passwordauthentication yes' <<< "$settings"; then
    warn 'G0 WARN: SSH password login is enabled. After key login works, set PasswordAuthentication no.'
  fi
}
# Rules first (SSH, 80, 443), then enable: an inactive UFW never cuts the current SSH session.
firewall_setup() {
  local port status
  have_cmd ufw || { warn 'G0: ufw is not installed.'; return 3; }
  while IFS= read -r port; do run_root ufw allow "$port/tcp" >/dev/null || return 3; done < <(ssh_ports)
  for port in 80 443; do run_root ufw allow "$port/tcp" >/dev/null || return 3; done
  status=$(run_root ufw status 2>/dev/null || true)
  if [[ $status == *'Status: active'* ]]; then return 0; fi
  if [[ ${OAOS_UFW_ENABLE:-1} == 0 ]]; then
    warn 'G0: UFW stays inactive because OAOS_UFW_ENABLE=0; verify reports it.'
    return 0
  fi
  run_root ufw --force enable >/dev/null || return 3
  info "Changed host setting: enabled UFW allowing SSH ($(ssh_ports | paste -sd, -)), 80 and 443. Undo with: sudo ufw disable"
}

do_prep() {
  local blocked=0 code pkg
  local missing=()
  private_dir "$(oaos_home)/.oaos"
  private_dir "$(oaos_home)/.oaos/logs"
  private_dir "$(oaos_home)/.oaos/backups"
  mkdir -p "$(oaos_home)/.oaos-install"
  code=0; host_capacity || code=$?
  ((code == 0)) || blocked=1
  code=0; port_precheck || code=$?
  ((code == 0)) || blocked=1
  for pkg in git curl xz-utils ca-certificates nginx certbot python3-certbot-nginx ufw rsync iproute2 openssl; do
    if ! have_cmd dpkg-query || ! native_installed "$pkg"; then missing+=("$pkg"); fi
  done
  if ((${#missing[@]})); then
    if have_cmd apt-get; then
      run_root apt-get update >/dev/null && run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}" >/dev/null || blocked=1
    else warn 'Package manager unavailable; required packages need manual installation.'; blocked=1; fi
  fi
  ssh_key_report
  firewall_setup || blocked=1
  platform_swap_prepare || blocked=1
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
  run_root systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null || blocked=1
  ((blocked == 0)) || return 3
}

installer_stale() { [[ ! -s $1 ]] || [[ -n $(find "$1" -mmin +1440 -print 2>/dev/null) ]]; }

do_hermes() {
  local installer cache answer actual_sha
  if have_cmd hermes && hermes --version >/dev/null 2>&1; then return 0; fi
  have_cmd curl || { warn 'curl is required to download Hermes.'; return 3; }
  cache="$(oaos_home)/.oaos/cache"
  mkdir -p "$cache"
  installer="$cache/hermes-install.sh"
  if installer_stale "$installer"; then
    rm -f -- "$installer"
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

# G1 passes only when a bounded model request answers (as in Personal).
do_llm() {
  local model
  need_hermes || return 3
  model=$(config_value model)
  if [[ -n $model && $model != null ]] && platform_chat_probe; then
    info 'G1: model probe answered.'
    return 0
  fi
  warn "G1: no working model yet. Choose a provider (https://opencode.ai/go or https://hermes-agent.nousresearch.com/docs/integrations/providers), then either run 'hermes setup --portal' or store the provider key with '$(secret_hint '<PROVIDER>_API_KEY')' and pick the model with 'hermes model'. Rerun this stage; it passes when a test request answers."
  return 3
}

do_telegram() {
  local token users result
  need_hermes || return 3
  token=$(config_value TELEGRAM_BOT_TOKEN)
  if [[ -z $token || $token == null ]]; then
    warn "G2: create a bot at https://t.me/BotFather (/newbot) and store its token with '$(secret_hint TELEGRAM_BOT_TOKEN)'."
    return 3
  fi
  if ! result=$(oaos_telegram_get_me "$token"); then
    token=''
    warn "G2: Telegram getMe failed ($result). Store the token again with '$(secret_hint TELEGRAM_BOT_TOKEN)' and retry; https://t.me/BotFather"
    return 3
  fi
  token=''
  users=$(config_value TELEGRAM_ALLOWED_USERS)
  if [[ -z $users || $users == null ]] || ! oaos_valid_user_ids "$users"; then
    warn "G3: get your numeric ID at https://t.me/userinfobot and store it (comma-separated numbers only) with '$(secret_hint TELEGRAM_ALLOWED_USERS)'."
    return 3
  fi
  restart_gateway_if_running 'Telegram'
}

# A gateway that is already installed and running must reload new credentials; before the
# gateway stage has installed it there is nothing to restart.
restart_gateway_if_running() {
  platform_gateway_service_present || return 0
  project_gateway_running || return 0
  hermes gateway restart >/dev/null 2>&1 || { warn "Gateway restart after $1 setup failed; run hermes gateway restart."; return 3; }
  info "Gateway restarted to load the $1 settings."
}

# Architecture §6: unattended surfaces (cron, single-query -q runs, webhooks) deny flagged actions.
ensure_unattended_deny() {
  local key value blocked=0
  for key in approvals.cron_mode approvals.single_query_mode approvals.unattended_mode; do
    value=$(config_value "$key")
    case $value in
      deny) ;;
      ''|null)
        if hermes config set "$key" deny >/dev/null; then info "Changed Hermes setting: $key=deny."
        else warn "Could not set $key=deny."; blocked=1; fi ;;
      *) warn "$key is '$value'; unattended runs could approve flagged actions. Run 'hermes config set $key deny' or keep it knowingly (verify reports FAIL)."; blocked=1 ;;
    esac
  done
  ((blocked == 0)) || return 3
}

do_gateway() {
  need_hermes || return 3
  if [[ $(stage_status telegram) != 'done' ]]; then
    warn 'Gateway waits for the telegram stage (G2/G3); starting it first would run without the bot.'
    return 3
  fi
  ensure_unattended_deny || return 3
  if ! project_gateway_running; then
    hermes gateway install --start-on-login --start-now >/dev/null || return 1
  fi
  platform_gateway_autostart ensure || return 3
  project_gateway_running || { warn 'Gateway is not running after install; inspect hermes gateway status.'; return 3; }
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
  git init -q -b main "$wiki"
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
  git -C "$wiki" -c user.name='OAOS Bootstrap' -c user.email='bootstrap@localhost' commit -qm 'wiki: seed project index'
}

do_harness() {
  local home name target
  home=$(platform_hermes_home)
  [[ -n $home && $home == /* ]] || { warn 'Hermes home could not be resolved; harness not written.'; return 3; }
  mkdir -p "$home/memories"
  for name in SOUL USER MEMORY; do
    if [[ $name == SOUL ]]; then target="$home/SOUL.md"; else target="$home/memories/$name.md"; fi
    if [[ ! -e $target ]]; then
      { printf '<!-- Draft — refine through conversation. Do not store secrets. -->\n'; cat "$repo_root/harness/templates/$name.md"; } > "$target"
    fi
  done
}

# The backup script shipped by earlier Project releases; it is upgraded in place.
legacy_backup_script() {
  cat <<'EOF'
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
}

# Installs editions/project/backup.sh as the cron script. An identical, legacy or
# OAOS-managed copy is replaced; anything else is an operator change and is preserved.
install_backup_script() {
  local script=$1 source="$repo_root/editions/project/backup.sh" staged
  if [[ -e $script || -L $script ]]; then
    cmp -s "$source" "$script" && return 0
    if ! legacy_backup_script | cmp -s - "$script" &&
       [[ $(sed -n 2p "$script" 2>/dev/null) != '# OAOS Project daily backup. Installed by editions/project/install.sh (cron stage) as' ]]; then
      warn "Existing script $script was changed by an operator; preserving it. Review it, then remove it and rerun cron."
      return 3
    fi
    info 'Upgrading the OAOS backup script (databases, service data, stack secrets).'
  fi
  staged=$(mktemp "$script.tmp.XXXXXX")
  cp -- "$source" "$staged" && chmod 700 "$staged" && mv -- "$staged" "$script" || { rm -f -- "$staged"; return 1; }
}

do_cron() {
  local scripts jobs name=oaos-daily-backup
  need_hermes || return 3
  scripts="$(platform_hermes_home)/scripts"
  mkdir -p "$scripts"
  private_dir "$(oaos_home)/.oaos/backups"
  install_backup_script "$scripts/$name.sh" || return $?
  jobs=$(hermes cron list --all 2>/dev/null) || return 3
  if ! grep -Fq "$name" <<< "$jobs"; then
    # The cron CLI resolves --script relative to the Hermes scripts directory.
    hermes cron create --name "$name" --script "$name.sh" --no-agent --deliver local '0 3 * * *' >/dev/null || return 1
  fi
  jobs=$(hermes cron list --all 2>/dev/null) || return 3
  grep -Fq "$name" <<< "$jobs" || return 3
}

# --- G6: DNS, nginx and TLS ----------------------------------------------------------------

public_ip() { curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true; }
dns_ok() {
  local answer
  answer=$(getent ahostsv4 "$1" 2>/dev/null | awk '{print $1}' || true)
  grep -Fqx "$2" <<< "$answer"
}
# Polls until every domain resolves to IP or OAOS_DNS_TIMEOUT seconds (default 600) pass.
dns_wait() {
  local ip=$1 timeout=${OAOS_DNS_TIMEOUT:-600} interval=${OAOS_DNS_INTERVAL:-15} deadline key pending
  [[ $timeout =~ ^[0-9]+$ ]] || timeout=600
  [[ $interval =~ ^[1-9][0-9]*$ ]] || interval=15
  deadline=$((SECONDS + timeout))
  while :; do
    pending=()
    for key in CHAT_DOMAIN NOTE_DOMAIN PORTAL_DOMAIN; do dns_ok "$(stack_get "$key")" "$ip" || pending+=("$(stack_get "$key")"); done
    ((${#pending[@]} == 0)) && return 0
    if ((SECONDS >= deadline)); then
      warn "G6: after ${timeout}s these A records still do not resolve to this server ($ip): ${pending[*]}. Add or fix them at the DNS provider, then rerun ingress."
      return 3
    fi
    info "G6: waiting for DNS (${pending[*]} -> $ip)..."
    sleep "$interval"
  done
}
render_vhost() {
  sed -e "s/__CHAT_DOMAIN__/$(stack_get CHAT_DOMAIN)/g" -e "s/__NOTE_DOMAIN__/$(stack_get NOTE_DOMAIN)/g" \
    -e "s/__PORTAL_DOMAIN__/$(stack_get PORTAL_DOMAIN)/g" -e "s@__PORTAL_ROOT__@$2@g" "$1"
}
do_ingress() {
  local ip key domain template target root email rendered hash current portal reinstall=() name sys
  sys=$(test_root)
  root=/var/www/oaos-portal
  if ! have_cmd nginx || ! have_cmd certbot; then warn 'nginx and certbot nginx plugin required; run prep.'; return 3; fi
  email=$(project_gate_value OAOS_ACME_EMAIL || project_gate_value OAOS_MAIL_ADDRESS || true)
  [[ $email =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || {
    warn "G6: no certificate e-mail. Store the G9 bot address with '$(secret_hint OAOS_MAIL_ADDRESS)' (used by default) or a separate one with '$(secret_hint OAOS_ACME_EMAIL)'."; return 3; }
  for key in CHAT_DOMAIN NOTE_DOMAIN PORTAL_DOMAIN; do
    [[ -n $(stack_get "$key") ]] || { warn "G6: $key is missing from the stack environment; run stack with the domain values first."; return 3; }
  done
  ip=$(public_ip)
  [[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { warn 'Public IPv4 lookup failed; check network and retry.'; return 3; }
  dns_wait "$ip" || return 3
  run_root install -d -m 755 "$sys$root" "$sys/etc/nginx/sites-available" "$sys/etc/nginx/sites-enabled" || return 3
  portal=$(printf '<!-- OAOS Project managed portal -->\n<!doctype html><html lang="ko"><meta charset="utf-8"><title>OAOS Project</title><h1>OAOS Project</h1><p><a href="https://%s">Mattermost</a> · <a href="https://%s">Outline</a></p></html>\n' "$(stack_get CHAT_DOMAIN)" "$(stack_get NOTE_DOMAIN)")
  current=$(run_root cat "$sys$root/index.html" 2>/dev/null || true)
  if [[ -z $current || ( $current == '<!-- OAOS Project managed portal -->'* && $current != "${portal%$'\n'}" ) ]]; then
    printf '%s\n' "$portal" | run_root tee "$sys$root/index.html" >/dev/null || return 3
  fi
  # Managed vhosts carry the hash of their rendered template. A new template or domain
  # re-renders the file; certbot then reinstalls the certificate into it.
  for name in chat note portal; do
    case $name in chat) domain=$(stack_get CHAT_DOMAIN);; note) domain=$(stack_get NOTE_DOMAIN);; portal) domain=$(stack_get PORTAL_DOMAIN);; esac
    template="$repo_root/editions/project/ingress/$name.conf"
    target="$sys/etc/nginx/sites-available/oaos-$name.conf"
    rendered=$(render_vhost "$template" "$root")
    hash=$(printf '%s\n' "$rendered" | sha256sum | awk '{print $1}')
    current=$(run_root cat "$target" 2>/dev/null || true)
    if [[ -n $current ]]; then
      if [[ $current != '# OAOS Project managed vhost'* ]]; then
        warn "Existing nginx vhost $target is not managed by OAOS; preserving it."; return 3
      fi
    fi
    if [[ -z $current || $current != *"# OAOS template sha256: $hash"* ]]; then
      { printf '%s\n' "$rendered" | sed -n 1p; printf '# OAOS template sha256: %s\n' "$hash"; printf '%s\n' "$rendered" | sed 1d; } |
        run_root tee "$target" >/dev/null || return 3
      [[ -z $current ]] || { info "Re-rendered $target (template or domain changed)."; reinstall+=("$domain"); }
    fi
    run_root ln -sfn "$target" "$sys/etc/nginx/sites-enabled/oaos-$name.conf" || return 3
  done
  run_root nginx -t >/dev/null || return 3
  run_root systemctl reload nginx || return 3
  for key in CHAT_DOMAIN NOTE_DOMAIN PORTAL_DOMAIN; do
    domain=$(stack_get "$key")
    if ! run_root openssl x509 -checkend 2592000 -noout -in "$sys/etc/letsencrypt/live/$domain/fullchain.pem" >/dev/null 2>&1 ||
       [[ " ${reinstall[*]} " == *" $domain "* ]]; then
      run_root certbot --nginx --non-interactive --agree-tos --reinstall --email "$email" -d "$domain" --redirect >/dev/null ||
        { warn "TLS issuance failed for $domain; check DNS, port 80, and certbot logs."; return 3; }
    fi
  done
  run_root nginx -t >/dev/null || return 3
  if ! systemctl is-enabled --quiet certbot.timer 2>/dev/null && ! systemctl is-enabled --quiet snap.certbot.renew.timer 2>/dev/null; then
    warn 'Certbot auto-renew timer is not enabled.'; return 3
  fi
}

# --- G9: bot mailbox -----------------------------------------------------------------------

himalaya_config() {
  local env_file
  env_file=$(oaos_hermes_env_file) || return 3
  cat <<EOF2
# OAOS Project managed Himalaya account (the password is read from the Hermes .env).
[accounts.oaos-bot]
default = true
email = "$1"
display-name = "OAOS Bot"
backend.type = "imap"
backend.host = "$2"
backend.port = 993
backend.encryption.type = "tls"
backend.login = "$1"
backend.auth.type = "password"
backend.auth.cmd = "sed -n 's/^OAOS_MAIL_PASSWORD=//p' $env_file"
message.send.backend.type = "smtp"
message.send.backend.host = "$3"
message.send.backend.port = 587
message.send.backend.encryption.type = "start-tls"
message.send.backend.login = "$1"
message.send.backend.auth.type = "password"
message.send.backend.auth.cmd = "sed -n 's/^OAOS_MAIL_PASSWORD=//p' $env_file"
EOF2
}

do_mail() {
  local address password imap smtp dir result key current
  for key in OAOS_MAIL_ADDRESS OAOS_MAIL_PASSWORD OAOS_MAIL_IMAP_HOST OAOS_MAIL_SMTP_HOST; do migrate_stack_secret "$key" || return $?; done
  address=$(project_gate_value OAOS_MAIL_ADDRESS || true)
  [[ -n $address ]] || { warn "G9: create a dedicated bot mailbox (not a person's), then store its address with '$(secret_hint OAOS_MAIL_ADDRESS)'."; return 3; }
  oaos_mail_valid_address "$address" || {
    warn 'G9: use a standard address with letters, digits, dot, underscore, plus, or hyphen.'; return 3; }
  imap=$(project_gate_value OAOS_MAIL_IMAP_HOST || project_mail_default_host imap "$address" || true)
  smtp=$(project_gate_value OAOS_MAIL_SMTP_HOST || project_mail_default_host smtp "$address" || true)
  [[ -n $imap && -n $smtp ]] || {
    warn "G9: store the provider's IMAP and SMTP hosts with '$(secret_hint OAOS_MAIL_IMAP_HOST)' and '$(secret_hint OAOS_MAIL_SMTP_HOST)' (Gmail addresses default to imap.gmail.com / smtp.gmail.com)."; return 3; }
  { oaos_mail_valid_host "$imap" && oaos_mail_valid_host "$smtp"; } || { warn 'G9: invalid IMAP or SMTP host name.'; return 3; }
  project_env_put OAOS_MAIL_IMAP_HOST "$imap" || return 1
  project_env_put OAOS_MAIL_SMTP_HOST "$smtp" || return 1
  password=$(project_secret_value OAOS_MAIL_PASSWORD || true)
  [[ -n $password ]] || {
    warn "G9: turn on 2-step verification for the bot mailbox, create an app password (Gmail: https://myaccount.google.com/apppasswords), and store it without spaces with '$(secret_hint OAOS_MAIL_PASSWORD)'."; return 3; }
  oaos_mail_valid_app_password "$password" || { password=''; warn "G9: the app password must be one word without spaces; store it again with '$(secret_hint OAOS_MAIL_PASSWORD)'."; return 3; }
  password=''
  result=$(project_mail_login imap "$address" "$imap") || {
    warn "G9: IMAP login to $imap failed ($result). Check the address, IMAP access and the app password, store it again with '$(secret_hint OAOS_MAIL_PASSWORD)', and rerun mail."; return 3; }
  result=$(project_mail_login smtp "$address" "$smtp") || {
    warn "G9: SMTP login to $smtp:587 failed ($result). Check the SMTP host and app password, then rerun mail."; return 3; }
  info 'G9: IMAP and SMTP logins succeeded.'
  have_cmd himalaya || { warn 'Install Himalaya from https://pimalaya.org/himalaya/ after verifying its release integrity, then rerun mail.'; return 3; }
  [[ $(himalaya --version 2>/dev/null) =~ [[:space:]]1\. ]] || { warn 'Himalaya configuration requires a verified 1.x CLI; configure this version manually.'; return 3; }
  dir="$(oaos_home)/.config/himalaya"
  private_dir "$dir"
  current=$(cat "$dir/config.toml" 2>/dev/null || true)
  if [[ -z $current || $current == '# OAOS Project managed Himalaya account'* || $current == *'oaos/stack/.env'* ]]; then
    himalaya_config "$address" "$imap" "$smtp" > "$dir/config.toml.tmp" && chmod 600 "$dir/config.toml.tmp" && mv -- "$dir/config.toml.tmp" "$dir/config.toml" || return 1
  else
    warn 'Existing Himalaya config preserved; verify the oaos-bot account manually.'
  fi
  if ! timeout 20s himalaya envelope list -a oaos-bot >/dev/null 2>&1; then
    warn 'G9: Himalaya IMAP login failed; review the Himalaya config.'; return 3
  fi
  if [[ -f $(stack_dir)/.env ]]; then
    native_outline_config || { warn 'Outline mail configuration failed.'; return 3; }
    if [[ $native_outline_changed == changed ]]; then
      run_root systemctl restart oaos-outline || { warn 'Outline mail configuration reload failed.'; return 3; }
    fi
  fi
}

# --- G7 / G8: Mattermost bot and Outline agent account -------------------------------------

write_onboarding() {
  cat > "$(oaos_home)/oaos/team-onboarding.md" <<EOF2
# OAOS Project 팀 온보딩

1. Mattermost 관리자(사람): https://$1 에서 팀을 만들고 멤버를 초대합니다.
2. Hermes 봇 계정(봇 전용, 시스템 관리자 아님)을 팀에 추가합니다. 허용할 멤버의 Mattermost 사용자 ID는 MATTERMOST_ALLOWED_USERS에 있습니다.
3. Outline 관리자(사람)가 먼저 https://$2 에 가입하고(최초 계정 = 워크스페이스 관리자), 이메일 로그인을 켠 뒤 AI 에이전트 계정(봇 메일함 주소)을 멤버·편집자로 초대합니다.
4. AI 에이전트 계정으로 로그인해 Settings → API Keys에서 발급한 토큰이 OUTLINE_API_TOKEN입니다. 에이전트가 만드는 문서는 이 계정 명의로 남습니다.
5. 허용 계정과 비허용 계정의 채팅 동작을 각각 확인합니다.
6. 봇 메일함의 송신·수신을 확인합니다.

자세한 절차는 editions/project/README.md의 "G7·G8 — 봇 계정 원칙"을 따릅니다.
EOF2
}

# The outline-wiki skill (search/read/create/update through the Outline API as the G8 agent
# account) is copied into the Hermes skills directory once G8 passes. A copy carrying the
# .oaos-managed marker is replaced on change; an owner's own copy is preserved.
install_outline_skill() {
  local source="$repo_root/skills/outline-wiki" skills target staged old
  skills="$(platform_hermes_home)/skills"
  target="$skills/outline-wiki"
  [[ -f $source/SKILL.md && -f $source/scripts/outline.py ]] || { warn 'skills/outline-wiki is missing from this checkout.'; return 1; }
  if [[ -e $target ]]; then
    diff -rq -x .oaos-managed -x __pycache__ "$source" "$target" >/dev/null 2>&1 && return 0
    if [[ ! -f $target/.oaos-managed ]]; then
      warn "An outline-wiki skill not installed by OAOS exists at $target; preserving it (verify runs its helper)."
      return 0
    fi
  fi
  mkdir -p "$skills"
  staged=$(mktemp -d "$skills/.outline-wiki.tmp.XXXXXX")
  cp -R "$source/." "$staged/" || { rm -rf -- "$staged"; return 1; }
  printf 'Installed by editions/project/install.sh (team stage) from skills/outline-wiki; local edits are replaced on the next team run.\n' > "$staged/.oaos-managed"
  chmod -R go-w "$staged"
  if [[ -e $target ]]; then
    old="$skills/.outline-wiki.old.$$"
    mv -- "$target" "$old" || { rm -rf -- "$staged"; return 1; }
    mv -- "$staged" "$target" || { mv -- "$old" "$target"; return 1; }
    rm -rf -- "$old"
  else
    mv -- "$staged" "$target" || { rm -rf -- "$staged"; return 1; }
  fi
  info "Installed the outline-wiki skill at $target (Outline search, read, create, update as the agent account)."
}

do_team() {
  local chat note output token users result mailbox outline_token admin=0 mmctl=/opt/mattermost/bin/mmctl
  need_hermes || return 3
  [[ ${OAOS_TEST_MODE:-0} != 1 || -z ${OAOS_TEST_MMCTL:-} ]] || mmctl=$OAOS_TEST_MMCTL
  chat=$(stack_get CHAT_DOMAIN)
  note=$(stack_get NOTE_DOMAIN)
  [[ -n $chat && -n $note ]] || { warn 'Stack domains absent; run stack first.'; return 3; }
  [[ -x $mmctl ]] || { warn 'Mattermost mmctl is absent from /opt/mattermost/bin.'; return 3; }
  output=$(run_root runuser -u mattermost -- "$mmctl" --local user list --role system_admin --json 2>/dev/null) || output=''
  if [[ -z $output ]]; then
    warn 'mmctl local mode unavailable; confirm the administrator in the Mattermost System Console.'
  else
    if printf '%s' "$output" | oaos_python -c 'import json,sys; x=json.load(sys.stdin); sys.exit(0 if len(x)>0 else 1)' 2>/dev/null; then admin=1; fi
    ((admin)) || { warn "G7: a person creates the first Mattermost administrator at https://$chat, then rerun team."; return 3; }
    run_root runuser -u mattermost -- "$mmctl" --local config set ServiceSettings.SiteURL "https://$chat" >/dev/null 2>&1 || warn 'mmctl SiteURL update unavailable; review Mattermost console.'
  fi
  migrate_stack_secret MATTERMOST_TOKEN || return $?
  token=$(project_secret_value MATTERMOST_TOKEN || true)
  [[ -n $token ]] || {
    warn "G7: in https://$chat System Console → Integrations → Bot Accounts, create a bot for Hermes (not a person's account, no System Admin role) and copy its token; store it with '$(secret_hint MATTERMOST_TOKEN)', then rerun team."; return 3; }
  if ! result=$(project_mattermost_bot_check "$token"); then
    token=''
    warn "G7: the Mattermost token was rejected ($result). Use a bot account token without System Admin; store it again with '$(secret_hint MATTERMOST_TOKEN)'."; return 3
  fi
  project_env_put MATTERMOST_URL "https://$chat" || return 1
  if [[ -n ${OAOS_MATTERMOST_ALLOWED_USERS:-} && -z ${MATTERMOST_ALLOWED_USERS:-} ]]; then
    MATTERMOST_ALLOWED_USERS=$OAOS_MATTERMOST_ALLOWED_USERS
  fi
  users=$(project_gate_value MATTERMOST_ALLOWED_USERS || true)
  if [[ -z $users ]] || ! project_valid_mattermost_ids "$users"; then
    token=''
    warn "G7: store the Mattermost user IDs allowed to talk to the bot (26-character IDs from System Console → Users, comma-separated) with '$(secret_hint MATTERMOST_ALLOWED_USERS)'."; return 3
  fi
  if ! result=$(project_mattermost_ids_exist "$token" "$users"); then
    token=''
    warn "G7: MATTERMOST_ALLOWED_USERS check failed ($result); store the correct IDs again."; return 3
  fi
  token=''
  write_onboarding "$chat" "$note"
  mailbox=$(project_gate_value OAOS_MAIL_ADDRESS || true)
  [[ -n $mailbox ]] || { warn 'G8 needs the G9 bot mailbox first (the Outline agent account uses that address); complete mail, then rerun team.'; return 3; }
  migrate_stack_secret OUTLINE_API_TOKEN || return $?
  [[ -z ${OAOS_OUTLINE_API_TOKEN:-} ]] || warn "OAOS_OUTLINE_API_TOKEN in the environment is ignored; store the token with '$(secret_hint OUTLINE_API_TOKEN)'."
  outline_token=$(project_secret_value OUTLINE_API_TOKEN || true)
  [[ -n $outline_token ]] || {
    warn "G8: a person signs up first at https://$note (the first account is the workspace admin), enables e-mail sign-in, and invites the AI-agent account $mailbox as a member or editor. Sign in as that agent account, create a token in Settings → API Keys, and store it with '$(secret_hint OUTLINE_API_TOKEN)'; then rerun team."; return 3; }
  if ! result=$(project_outline_agent_check "$outline_token" "$mailbox"); then
    outline_token=''
    warn "G8: the Outline token was rejected ($result). Issue it from the AI-agent account ($mailbox, member or editor, not admin) and store it again with '$(secret_hint OUTLINE_API_TOKEN)'."; return 3
  fi
  outline_token=''
  project_env_put OUTLINE_URL "https://$note" || return 1
  install_outline_skill || return 1
  restart_gateway_if_running 'Mattermost and Outline'
}

# project-verify.sh exit: 0 all PASS; 2 MANUAL pending, no FAIL; 1 FAIL; 3 blocked.
do_verify() { bash "$repo_root/bootstrap/verify/project-verify.sh"; }

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

# Severity order for the final exit code: 1 (error/FAIL) > 3 (blocked) > 2 (MANUAL) > 0.
severity() { case $1 in 1) printf 3 ;; 3) printf 2 ;; 2) printf 1 ;; *) printf 0 ;; esac; }
result_code=0
note_result() { if (($(severity "$1") > $(severity "$result_code"))); then result_code=$1; fi; }

for stage in "${stages[@]}"; do
  selected_stage "$stage" || continue
  if [[ $stage == verify ]] && ((skip_verify)); then info 'Skipping verify by request.'; continue; fi
  if [[ $stage != verify ]] && ((recheck == 0)) && stage_done "$stage"; then info "Skipping $stage (done)."; continue; fi
  if ((OAOS_DRY_RUN)); then info "Would run $stage (current: $(stage_status "$stage"))."; continue; fi
  info "Running $stage."
  # Each stage runs in a subshell with errexit active, outside any `if`/`||` context,
  # so an unguarded failing command fails the stage closed.
  set +e
  ( set -Eeuo pipefail; run_stage "$stage" )
  code=$?
  set -e
  if [[ $stage == verify ]]; then
    case $code in
      0) stage_mark verify verified 'All automated checks PASS'; info 'verify: all checks PASS.' ;;
      2) stage_mark verify applied 'No FAIL; MANUAL checks pending human evidence'; warn 'verify: no FAIL, but MANUAL checks are pending (exit 2).' ;;
      3) stage_mark verify blocked 'Verifier could not run'; warn 'verify blocked.' ;;
      *) code=1; stage_mark verify failed 'One or more checks FAIL'; warn 'verify: one or more checks FAIL.' ;;
    esac
    note_result "$code"
    continue
  fi
  case $code in
    0) stage_mark "$stage" 'done' 'Completed'; info "$stage done." ;;
    3) stage_mark "$stage" blocked 'Needs a gate, prerequisite, or permission'; warn "$stage blocked."; note_result 3 ;;
    *) stage_mark "$stage" failed 'Command failed; review the install log'; warn "$stage failed."; note_result 1 ;;
  esac
done
show_table
exit "$result_code"
