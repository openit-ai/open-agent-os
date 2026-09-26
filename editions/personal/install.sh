#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"

stages=(prep hermes llm telegram gateway wiki harness cron verify)
selected=()
OAOS_DRY_RUN=0
yes=0
skip_verify=0
show_status=0
timezone=''

usage() {
  cat <<'EOF'
Usage: bash editions/personal/install.sh [--dry-run] [--stage name[,name...]] [--status] [--yes] [--timezone Area/City] [--skip-verify] [--help]

Stages: prep, hermes, llm, telegram, gateway, wiki, harness, cron, verify.
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
info 'Personal installation started.'
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
  for pkg in git curl xz-utils ca-certificates; do
    if ! have_cmd dpkg-query || ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then missing+=("$pkg"); fi
  done
  if ((${#missing[@]})); then
    if have_cmd apt-get; then
      run_root apt-get update && run_root apt-get install -y "${missing[@]}" || blocked=1
    else warn 'Package manager unavailable; required packages need manual installation.'; blocked=1; fi
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
  if [[ ! -f /etc/systemd/logind.conf.d/oaos-personal.conf ]]; then
    if run_root install -d -m 755 /etc/systemd/logind.conf.d; then
      printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nIdleAction=ignore\n' |
        run_root tee /etc/systemd/logind.conf.d/oaos-personal.conf >/dev/null || blocked=1
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
  if ! hermes gateway status 2>/dev/null | grep -Eiq 'running|active'; then
    hermes gateway install --start-on-login --start-now >/dev/null || return 1
  fi
  if ! systemctl --user is-enabled hermes-gateway >/dev/null 2>&1; then
    warn 'Gateway user service is not enabled.'; return 3
  fi
  if [[ $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true) != yes ]]; then
    run_root loginctl enable-linger "$(id -un)" || return 3
  fi
  hermes gateway status 2>/dev/null | grep -Eiq 'running|active' || return 3
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
# Personal Wiki

Purpose: keep personal knowledge in a local, versioned workspace.

## Areas

- Inbox
- Projects
- People
- Resources
- Archive
EOF
  printf '# Personal Wiki\n\nStart at [index.md](index.md). Keep private information out of public remotes.\n' > "$wiki/README.md"
  printf '.DS_Store\n*.swp\n' > "$wiki/.gitignore"
  git -C "$wiki" add index.md README.md .gitignore
  git -C "$wiki" -c user.name='OAOS Bootstrap' -c user.email='bootstrap@localhost' commit -qm 'wiki: seed personal index' || return 1
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
  local scripts jobs name script
  scripts="$(oaos_home)/.hermes/scripts"
  need_hermes || return 3
  mkdir -p "$scripts"
  jobs=$(hermes cron list --all 2>/dev/null) || return 3
  for name in oaos-daily-backup oaos-gateway-watchdog; do
    if grep -Fq "$name" <<< "$jobs"; then continue; fi
    script="$scripts/$name.sh"
    if [[ -e $script ]]; then warn "Existing script for $name needs review; preserving it."; return 3; fi
    if [[ $name == oaos-daily-backup ]]; then
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
    else
      cat > "$script" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
if ! systemctl --user is-active --quiet hermes-gateway; then
  systemctl --user start hermes-gateway
fi
EOF
      chmod 700 "$script"
      hermes cron create --name "$name" --script "$script" --no-agent --deliver local '*/5 * * * *' >/dev/null || return 1
    fi
  done
  jobs=$(hermes cron list --all 2>/dev/null) || return 3
  grep -Fq 'oaos-daily-backup' <<< "$jobs" && grep -Fq 'oaos-gateway-watchdog' <<< "$jobs" || return 3
}

do_verify() { bash "$repo_root/bootstrap/verify/personal-verify.sh"; }

# Stage dispatch is deliberately explicit: no user-controlled command name is evaluated.
run_stage() {
  case $1 in
    prep) do_prep ;; hermes) do_hermes ;; llm) do_llm ;;
    telegram) do_telegram ;; gateway) do_gateway ;; wiki) do_wiki ;;
    harness) do_harness ;; cron) do_cron ;; verify) do_verify ;;
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
  run_stage "$stage" || code=$?
  case $code in
    0) stage_mark "$stage" 'done' 'Completed'; info "$stage done." ;;
    3) stage_mark "$stage" blocked 'Needs a gate, prerequisite, or permission'; warn "$stage blocked."; ((result == 1)) || result=3 ;;
    *) stage_mark "$stage" failed 'Command failed; review the install log'; warn "$stage failed."; result=1 ;;
  esac
done
show_table
exit "$result"
