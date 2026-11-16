#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"
# shellcheck source=bootstrap/lib/platform.sh
. "$repo_root/bootstrap/lib/platform.sh"
# shellcheck source=bootstrap/lib/secret.sh
. "$repo_root/bootstrap/lib/secret.sh"

stages=(prep hermes llm telegram gateway wiki harness cron verify)
export OAOS_STATE_EDITION=personal
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
Usage: bash editions/personal/install.sh [--dry-run] [--stage name[,name...]] [--status] [--yes]
                                         [--recheck] [--reverify] [--timezone Area/City] [--skip-verify] [--help]

Stages: prep, hermes, llm, telegram, gateway, wiki, harness, cron, verify.
The default runs all stages in order. Completed stages are skipped on rerun,
except verify, which always runs again.
--recheck   re-run selected stages even when recorded as done (each is idempotent).
--reverify  run only the verify stage (same as --stage verify).
--dry-run   prints a plan without writing state or changing the system.
--yes       approves running the downloaded official Hermes installer; pass it only
            after the user has approved that choice gate.
Secrets are stored with: bash bootstrap/bin/oaos-secret-set KEY (hidden prompt; never argv).
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
oaos_repo_owner_check "$repo_root" || exit 3
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
secret_hint() { printf 'bash bootstrap/bin/oaos-secret-set %s' "$1"; }

# Owner-only directories for OAOS logs and backups (mode 700; NTFS keeps profile ACLs).
private_dir() {
  mkdir -p "$1"
  [[ $OAOS_PLATFORM == windows-gitbash ]] || chmod 700 "$1"
}

do_prep() {
  local blocked=0 pkg
  local missing=()
  platform_require_tools || return 3
  private_dir "$(platform_path logs)"
  private_dir "$(platform_path backups)"
  mkdir -p "$(dirname "$(platform_path state)")"
  while IFS= read -r pkg; do
    if [[ -n $pkg ]]; then missing+=("$pkg"); fi
  done < <(platform_package_prereqs)
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

# A cached installer is reused only while fresh, so the copy the user reviewed is the
# copy that runs after approval; anything older is downloaded again.
installer_stale() { [[ ! -s $1 ]] || [[ -n $(find "$1" -mmin +1440 -print 2>/dev/null) ]]; }

do_hermes() {
  local installer installer_native cache answer actual_sha native url
  if have_cmd hermes && hermes --version >/dev/null 2>&1; then return 0; fi
  have_cmd curl || { warn 'curl is required to download Hermes.'; return 3; }
  if [[ $OAOS_PLATFORM == windows-gitbash && -z ${OAOS_HERMES_INSTALLER_SHA256:-} ]]; then
    warn 'Windows: the official install.ps1 runs only with a pinned checksum. Download https://hermes-agent.nousresearch.com/install.ps1, review it, then rerun with OAOS_HERMES_INSTALLER_SHA256=<sha256>.'
    return 3
  fi
  cache=$(platform_path cache)
  mkdir -p "$cache"
  if [[ $OAOS_PLATFORM == windows-gitbash ]]; then
    installer="$cache/hermes-install.ps1"; url='https://hermes-agent.nousresearch.com/install.ps1'
  else
    installer="$cache/hermes-install.sh"; url='https://hermes-agent.nousresearch.com/install.sh'
  fi
  installer_native=$(oaos_native_path "$installer") || return 3
  if installer_stale "$installer"; then
    rm -f -- "$installer"
    curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 10 --retry 2 "$url" -o "$installer_native" ||
      { rm -f -- "$installer"; warn 'Hermes installer download failed.'; return 1; }
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

# G1 succeeds only when a bounded model request answers; an API key alone is not proof,
# and OAuth/portal credentials (hermes setup --portal, hermes model) count equally.
do_llm() {
  local model
  need_hermes || return 3
  model=$(config_value model)
  if [[ -n $model && $model != null ]] && platform_chat_probe; then
    info 'G1: model probe answered.'
    return 0
  fi
  warn "G1: no working model yet. Choose a provider (https://opencode.ai/go or https://hermes-agent.nousresearch.com/docs/integrations/providers), then either run 'hermes setup --portal' (Nous Portal OAuth) or store the provider key with '$(secret_hint '<PROVIDER>_API_KEY')' (for example OPENCODE_GO_API_KEY, OPENROUTER_API_KEY, AI_GATEWAY_API_KEY) and pick the model with 'hermes model'. Rerun this stage; it passes when a test request answers."
  return 3
}

do_telegram() {
  local token users result
  need_hermes || return 3
  token=$(config_value TELEGRAM_BOT_TOKEN)
  users=$(config_value TELEGRAM_ALLOWED_USERS)
  if [[ -z $token || $token == null ]]; then
    warn "G2: create a bot at https://t.me/BotFather (/newbot) and store its token with '$(secret_hint TELEGRAM_BOT_TOKEN)'."
    return 3
  fi
  if ! result=$(oaos_telegram_get_me "$token"); then
    token=''
    warn "G2: Telegram getMe failed ($result). Check the bot token or network, store it again with '$(secret_hint TELEGRAM_BOT_TOKEN)', and retry; https://t.me/BotFather"
    return 3
  fi
  token=''
  if [[ -z $users || $users == null ]] || ! oaos_valid_user_ids "$users"; then
    warn "G3: get your numeric ID at https://t.me/userinfobot and store it (comma-separated numbers only) with '$(secret_hint TELEGRAM_ALLOWED_USERS)'."
    return 3
  fi
  # A gateway started before Telegram was configured must reload the new credentials.
  if platform_gateway_status >/dev/null 2>&1; then
    hermes gateway restart >/dev/null 2>&1 || { warn 'Gateway restart after Telegram setup failed; run hermes gateway restart.'; return 3; }
    info 'Gateway restarted to load the Telegram settings.'
  fi
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
  if ! platform_gateway_status || ! platform_gateway_autostart registered; then
    platform_gateway_install || return $?
  fi
  platform_gateway_autostart ensure || return 3
  platform_gateway_status || return 3
}

install_wiki_hook() {
  local hook="$1/.git/hooks/pre-commit" tmp
  mkdir -p "$1/.git/hooks"
  tmp=$(mktemp "$hook.tmp.XXXXXX")
  cat > "$tmp" <<'HOOK'
#!/usr/bin/env bash
# OAOS: block commits that add secret-like values. Keep patterns in sync with
# bootstrap/lib/common.sh redact. Bypass only after review: git commit --no-verify.
set -euo pipefail
if git diff --cached -U0 --no-color | grep -E '^\+' | grep -Ev '^\+\+\+ ' |
   grep -Eiq '(sk-|ghp_|nous_|AIza|xoxb-|AKIA|vck_)[A-Za-z0-9_-]{8,}|[0-9]{8,10}:[A-Za-z0-9_-]{35,}|Bearer[[:space:]]+[A-Za-z0-9._~+/=-]{8,}|(api[_-]?key|token|secret|password)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"']?[^[:space:]"'"'"']{8,}'; then
  printf 'BLOCKED: staged changes contain a secret-like value; remove it before committing.\n' >&2
  exit 1
fi
HOOK
  if [[ -e $hook ]]; then
    if cmp -s "$tmp" "$hook"; then rm -f -- "$tmp"; return 0; fi
    rm -f -- "$tmp"
    warn 'Wiki already has a different pre-commit hook; preserving it (secret check not installed).'
    return 0
  fi
  chmod 700 "$tmp"
  mv -- "$tmp" "$hook"
}

do_wiki() {
  local wiki wiki_native
  wiki=$(platform_path wiki)
  [[ -n $wiki ]] || return 3
  wiki_native=$(oaos_native_path "$wiki") || return 3
  if [[ -e $wiki ]]; then
    [[ -d $wiki/.git ]] || { warn 'Wiki path already exists but is not a git repository; preserving it.'; return 3; }
    git -C "$wiki_native" log -1 --format=%h >/dev/null 2>&1 || { warn 'Existing wiki has no commit; preserving it.'; return 3; }
    install_wiki_hook "$wiki"
    return 0
  fi
  mkdir -p "$(dirname "$wiki")"
  git init -q -b main "$wiki_native"
  install_wiki_hook "$wiki"
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
  git -C "$wiki_native" -c user.name='OAOS Bootstrap' -c user.email='bootstrap@localhost' commit -qm 'wiki: seed personal index'
}

# Host facts the installer knows are written into MEMORY.md; the rest stays as
# {{...}} placeholders for the seeding conversation (harness/seeding.md).
memory_facts() {
  local os service
  case $OAOS_PLATFORM in
    linux) os="Linux $(uname -m)"; service='a systemd user service (hermes-gateway) with lingering; watchdog: oaos-gateway-watchdog.timer' ;;
    macos) os="macOS $(uname -m)"; service='a launchd agent; watchdog: ai.openit.oaos.gateway-watchdog launchd agent' ;;
    *) os="Windows (Git Bash) $(uname -m)"; service='the Hermes ONLOGON scheduled task' ;;
  esac
  printf -- '- Host runs %s; HERMES_HOME is %s; the wiki is a git repo at %s; backups go to %s.\n' \
    "$os" "$1" "$(platform_path wiki)" "$(platform_path backups)"
  printf -- '- The Telegram gateway runs as %s.\n' "$service"
}

do_harness() {
  local home name target facts
  home=$(platform_hermes_home)
  [[ -n $home && $home == /* ]] || { warn 'Hermes home could not be resolved; harness not written.'; return 3; }
  mkdir -p "$home/memories"
  facts=$(memory_facts "$home")
  for name in SOUL USER MEMORY; do
    if [[ $name == SOUL ]]; then target="$home/SOUL.md"; else target="$home/memories/$name.md"; fi
    [[ ! -e $target ]] || continue
    {
      printf '<!-- Draft — refine through conversation. Do not store secrets. -->\n'
      if [[ $name == MEMORY ]]; then
        OAOS_MEMORY_FACTS=$facts awk '
          /^- \{\{host \/ OS/ { print ENVIRON["OAOS_MEMORY_FACTS"]; next }
          /^- \{\{services and ports/ { next }
          { print }
        ' "$repo_root/harness/templates/MEMORY.md"
      else
        cat "$repo_root/harness/templates/$name.md"
      fi
    } > "$target"
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

write_backup_script() {
  local hermes_home hermes_parent hermes_leaf wiki python
  hermes_home=$(platform_hermes_home)
  hermes_parent=$(dirname "$hermes_home")
  hermes_leaf=$(basename "$hermes_home")
  wiki=$(platform_path wiki)
  python=$(platform_python)
  printf '#!/usr/bin/env bash\nset -Eeuo pipefail\numask 077\n'
  printf 'hermes_parent=%q\nhermes_leaf=%q\nwiki_parent=%q\nwiki_leaf=%q\npython=%q\n' \
    "$hermes_parent" "$hermes_leaf" "$(dirname "$wiki")" "$(basename "$wiki")" "$python"
  cat <<'EOF'
backup_dir="$HOME/.oaos/backups"
mkdir -p "$backup_dir"
case $(uname -s) in MINGW*|MSYS*|CYGWIN*) ;; *) chmod 700 "$backup_dir" ;; esac
target="$backup_dir/oaos-$(date +%Y%m%d-%H%M%S).tar.gz"
tmp=$(mktemp "$target.tmp.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
tar -czf "$tmp" -C "$hermes_parent" "$hermes_leaf" -C "$wiki_parent" "$wiki_leaf"
chmod 600 "$tmp"
mv -- "$tmp" "$target"
case $(uname -s) in MINGW*|MSYS*|CYGWIN*) backup_dir=$(cygpath -w "$backup_dir") ;; esac
"$python" - "$backup_dir" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
files = sorted(root.glob('oaos-*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True)
for path in files[7:]:
    path.unlink()
PY
EOF
}

write_watchdog_script() {
  local hermes_bin
  hermes_bin=$(command -v hermes)
  printf '#!/usr/bin/env bash\n# OAOS gateway watchdog, run by an OS timer (not Hermes cron, which lives in the gateway).\nset -Eeuo pipefail\n'
  printf 'hermes=%q\n' "$hermes_bin"
  cat <<'EOF'
status=$("$hermes" gateway status 2>/dev/null || true)
if printf '%s\n' "$status" | grep -Ei '(^|[[:space:]:])(running|active)([[:space:]]|$)' >/dev/null &&
   ! printf '%s\n' "$status" | grep -Ei 'not running|inactive|stopped' >/dev/null; then
  exit 0
fi
"$hermes" gateway start
EOF
}

do_cron() {
  local scripts bin jobs script script_tmp code=0
  need_hermes || return 3
  scripts=$(platform_path scripts)
  bin=$(platform_path bin)
  mkdir -p "$scripts"
  private_dir "$bin"
  private_dir "$(platform_path backups)"
  jobs=$(hermes cron list --all 2>/dev/null) || return 3
  # Daily backup: a no-agent Hermes cron script (runs while the gateway is up).
  if ! grep -Fq oaos-daily-backup <<< "$jobs"; then
    script="$scripts/oaos-daily-backup.sh"
    script_tmp=$(mktemp "$script.tmp.XXXXXX")
    write_backup_script > "$script_tmp" || { rm -f -- "$script_tmp"; return 1; }
    install_personal_cron_script "$script_tmp" "$script" oaos-daily-backup || return $?
    hermes cron create --name oaos-daily-backup --script "$(basename "$script")" --no-agent --deliver local '0 3 * * *' >/dev/null
  fi
  # Gateway watchdog: an OS timer, because Hermes cron cannot fire while the gateway is down.
  script="$bin/oaos-gateway-watchdog.sh"
  script_tmp=$(mktemp "$script.tmp.XXXXXX")
  write_watchdog_script > "$script_tmp" || { rm -f -- "$script_tmp"; return 1; }
  install_personal_cron_script "$script_tmp" "$script" oaos-gateway-watchdog || code=$?
  ((code == 0)) || return "$code"
  platform_watchdog_install "$script" || return $?
  if grep -Fq oaos-gateway-watchdog <<< "$jobs"; then
    warn 'A legacy Hermes cron job oaos-gateway-watchdog exists; remove it (hermes cron list, then hermes cron remove <id>). The OS timer replaces it.'
  fi
  jobs=$(hermes cron list --all 2>/dev/null) || return 3
  grep -Fq oaos-daily-backup <<< "$jobs" || return 3
  code=0
  platform_watchdog_check || code=$?
  [[ $code == 0 || $code == 4 ]] || return 3
}

# personal-verify.sh exit: 0 all PASS; 2 MANUAL pending, no FAIL; 1 FAIL; 3 blocked.
do_verify() { bash "$repo_root/bootstrap/verify/personal-verify.sh"; }

# Stage dispatch is deliberately explicit: no user-controlled command name is evaluated.
run_stage() {
  case $1 in
    prep) do_prep ;; hermes) do_hermes ;; llm) do_llm ;;
    telegram) do_telegram ;; gateway) do_gateway ;; wiki) do_wiki ;;
    harness) do_harness ;; cron) do_cron ;; verify) do_verify ;;
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
  # Each stage runs in a subshell with errexit active. It must not sit in an `if`,
  # `||` or `&&` context, which would silently disable errexit inside the stage, so
  # any unguarded failing command fails the stage closed.
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
