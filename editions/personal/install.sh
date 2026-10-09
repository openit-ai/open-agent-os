#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"
# shellcheck source=bootstrap/lib/platform.sh
. "$repo_root/bootstrap/lib/platform.sh"

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
os_detect >/dev/null || exit 3
if [[ $OAOS_PLATFORM != linux && $OAOS_DRY_RUN != 1 ]]; then
  platform_python >/dev/null || exit 3
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
if (( ! OAOS_DRY_RUN )) && ! state_valid; then
  corrupt="$(oaos_state_file).corrupt-$(date +%s)"
  [[ ! -e $corrupt ]] || die 'Corrupt state backup already exists; retry later.'
  mv -- "$(oaos_state_file)" "$corrupt" || die 'Failed to preserve corrupt state file.'
  warn "Invalid state file preserved at $corrupt; starting fresh."
fi
info 'Personal installation started.'
platform_check_os

selected_stage() {
  local item
  ((${#selected[@]} == 0)) && return 0
  for item in "${selected[@]}"; do [[ $item == "$1" ]] && return 0; done
  return 1
}

need_hermes() { have_cmd hermes || { warn 'Hermes is unavailable; complete the hermes stage first.'; return 3; }; }
config_value() { hermes config get --raw "$1" 2>/dev/null || true; }

do_prep() {
  local blocked=0 pkg
  local missing=()
  platform_require_tools || return 3
  mkdir -p "$(platform_path logs)" "$(platform_path backups)" "$(dirname "$(platform_path state)")"
  while IFS= read -r pkg; do [[ -n $pkg ]] && missing+=("$pkg"); done < <(platform_package_prereqs)
  if ((${#missing[@]})); then
    platform_package_install "${missing[@]}" || blocked=1
  fi
  platform_swap_prepare || blocked=1
  if [[ -n $timezone ]]; then
    if [[ $(platform_timezone_get 2>/dev/null || true) != "$timezone" ]]; then
      platform_timezone_set "$timezone" || blocked=1
    fi
  fi
  platform_sleep_policy || blocked=1
  ((blocked == 0)) || return 3
}

do_hermes() {
  local installer installer_native cache answer actual_sha native
  if have_cmd hermes && hermes --version >/dev/null 2>&1; then return 0; fi
  have_cmd curl || { warn 'curl is required to download Hermes.'; return 3; }
  cache=$(platform_path cache)
  mkdir -p "$cache"
  if [[ $OAOS_PLATFORM == windows-gitbash ]]; then installer="$cache/hermes-install.ps1"
  else installer="$cache/hermes-install.sh"; fi
  installer_native=$(oaos_native_path "$installer") || return 3
  if [[ ! -e $installer ]]; then
    if [[ $OAOS_PLATFORM == windows-gitbash ]]; then
      curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 10 --retry 2 'https://hermes-agent.nousresearch.com/install.ps1' -o "$installer_native" || { rm -f -- "$installer"; warn 'Hermes installer download failed.'; return 1; }
    else
      curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 10 --retry 2 'https://hermes-agent.nousresearch.com/install.sh' -o "$installer_native" || { rm -f -- "$installer"; warn 'Hermes installer download failed.'; return 1; }
    fi
  fi
  if [[ ! -s $installer ]]; then rm -f -- "$installer"; warn 'Hermes installer is empty.'; return 1; fi
  if [[ $OAOS_PLATFORM == windows-gitbash ]]; then
    native=$(platform_windows_convert_path to-native "$installer") || return 3
    # shellcheck disable=SC2016 # PowerShell variables are intentionally literal.
    OAOS_INSTALLER_NATIVE_PATH=$native platform_windows_ps '$tokens=$null; $errors=$null; [void][System.Management.Automation.Language.Parser]::ParseFile($env:OAOS_INSTALLER_NATIVE_PATH,[ref]$tokens,[ref]$errors); if ($errors.Count -gt 0) { exit 1 }' >/dev/null || { rm -f -- "$installer"; warn 'Downloaded PowerShell installer failed syntax validation.'; return 1; }
  else
    bash -n "$installer" || { rm -f -- "$installer"; warn 'Downloaded installer failed syntax validation.'; return 1; }
  fi
  actual_sha=$(platform_sha256 "$installer") || return 1
  [[ $actual_sha =~ ^[[:xdigit:]]{64}$ ]] || { warn 'Hermes installer SHA-256 could not be verified.'; return 3; }
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
  platform_install_hermes "$installer" || return $?
  hash -r
  if ! have_cmd hermes || ! hermes --version >/dev/null 2>&1; then
    warn 'Hermes is not on PATH after installation; open a new shell.'
    return 3
  fi
}

do_llm() {
  local key value model provider provider_key
  local keys=(NOUS_API_KEY OPENROUTER_API_KEY OPENCODE_API_KEY VERCEL_AI_GATEWAY_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY)
  need_hermes || return 3
  model=$(config_value model)
  if [[ -n $model && $model != null ]]; then
    provider=$(hermes config get model 2>/dev/null | awk -F: '
      tolower($1) ~ /^[[:space:]]*provider[[:space:]]*$/ {
        name = $2
        gsub(/\r/, "", name)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
        if (name ~ /^[A-Za-z0-9][A-Za-z0-9_-]*$/) { print name; exit }
      }
    ' || true)
    if [[ -n $provider ]]; then
      provider_key=$(printf '%s' "${provider//-/_}" | tr '[:lower:]' '[:upper:]')_API_KEY
      keys=("$provider_key" "${keys[@]}")
    fi
    for key in "${keys[@]}"; do
      value=$(config_value "$key")
      if [[ -n $value && $value != null ]]; then return 0; fi
    done
  fi
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
  if ! platform_gateway_status || ! platform_gateway_autostart registered; then
    platform_gateway_install || return $?
  fi
  platform_gateway_autostart ensure || return 3
  platform_gateway_status || return 3
}

do_wiki() {
  local wiki wiki_native
  wiki=$(platform_path wiki)
  wiki_native=$(oaos_native_path "$wiki") || return 3
  if [[ -e $wiki ]]; then
    [[ -d $wiki/.git ]] || { warn 'Wiki path already exists but is not a git repository; preserving it.'; return 3; }
    git -C "$wiki_native" log -1 --format=%h >/dev/null 2>&1 || { warn 'Existing wiki has no commit; preserving it.'; return 3; }
    return 0
  fi
  mkdir -p "$(dirname "$wiki")"
  git init -q -b main "$wiki_native" || return 1
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
  git -C "$wiki_native" add index.md README.md .gitignore
  git -C "$wiki_native" -c user.name='OAOS Bootstrap' -c user.email='bootstrap@localhost' commit -qm 'wiki: seed personal index' || return 1
}

do_harness() {
  local home name target
  home=$(platform_hermes_home)
  mkdir -p "$home/memories"
  for name in SOUL USER MEMORY; do
    if [[ $name == SOUL ]]; then target="$home/SOUL.md"; else target="$home/memories/$name.md"; fi
    if [[ ! -e $target ]]; then
      { printf '<!-- Draft — refine through conversation. Do not store secrets. -->\n'; cat "$repo_root/harness/templates/$name.md"; } > "$target" || return 1
    fi
  done
  info 'Credential typing stays vault-only (upstream default). Owner opt-in: bash editions/personal/computer-use-policy.sh enable — see docs/computer-use-credential-typing-v1.0.md.'
}

install_personal_cron_script() {
  local candidate=$1 script=$2 name=$3
  if [[ -e $script || -L $script ]]; then
    if cmp -s "$candidate" "$script"; then
      rm -f -- "$candidate"
      return 0
    fi
    rm -f -- "$candidate"
    warn "Existing script for $name needs review; preserving it."
    return 3
  fi
  chmod 700 "$candidate" || { rm -f -- "$candidate"; return 1; }
  mv -- "$candidate" "$script" || { rm -f -- "$candidate"; return 1; }
}

do_cron() {
  local scripts jobs name script script_tmp schedule hermes_home hermes_parent hermes_leaf python
  scripts=$(platform_path scripts)
  need_hermes || return 3
  mkdir -p "$scripts"
  jobs=$(hermes cron list --all 2>/dev/null) || return 3
  for name in oaos-daily-backup oaos-gateway-watchdog; do
    if grep -Fq "$name" <<< "$jobs"; then continue; fi
    script="$scripts/$name.sh"
    script_tmp=$(mktemp "$script.tmp.XXXXXX") || return 1
    if [[ $name == oaos-daily-backup ]]; then
      schedule='0 3 * * *'
      if [[ $OAOS_PLATFORM == linux ]]; then
        cat > "$script_tmp" <<'EOF' || { rm -f -- "$script_tmp"; return 1; }
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
      else
        hermes_home=$(platform_hermes_home) || { rm -f -- "$script_tmp"; return 3; }
        hermes_parent=$(dirname "$hermes_home")
        hermes_leaf=$(basename "$hermes_home")
        python=$(platform_python) || { rm -f -- "$script_tmp"; return 3; }
        {
          printf '#!/usr/bin/env bash\nset -Eeuo pipefail\numask 077\n'
          printf 'hermes_parent=%q\nhermes_leaf=%q\npython=%q\n' "$hermes_parent" "$hermes_leaf" "$python"
          cat <<'EOF'
mkdir -p "$HOME/.oaos/backups"
target="$HOME/.oaos/backups/oaos-$(date +%Y%m%d-%H%M%S).tar.gz"
tmp=$(mktemp "$target.tmp.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
tar -czf "$tmp" -C "$hermes_parent" "$hermes_leaf" -C "$HOME/data" wiki
mv -- "$tmp" "$target"
backup_dir="$HOME/.oaos/backups"
case $(uname -s) in MINGW*|MSYS*|CYGWIN*) backup_dir=$(cygpath -w "$backup_dir") ;; esac
"$python" - "$backup_dir" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
files = sorted(root.glob('oaos-*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True)
for path in files[7:]:
    path.unlink()
PY
EOF
        } > "$script_tmp" || { rm -f -- "$script_tmp"; return 1; }
      fi
    else
      schedule='*/5 * * * *'
      cat > "$script_tmp" <<'EOF' || { rm -f -- "$script_tmp"; return 1; }
#!/usr/bin/env bash
set -Eeuo pipefail
if ! hermes gateway status 2>/dev/null | grep -Eiq 'running|active'; then
  hermes gateway start
fi
EOF
    fi
    install_personal_cron_script "$script_tmp" "$script" "$name" || return $?
    hermes cron create --name "$name" --script "$(basename "$script")" --no-agent --deliver local "$schedule" >/dev/null || return 1
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
