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

stages=(prep hermes llm telegram gateway wiki harness cron mail verify)
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

Stages: prep, hermes, llm, telegram, gateway, wiki, harness, cron, mail, verify.
The default runs all stages in order. Completed stages are skipped on rerun,
except verify, which always runs again.
mail is the optional G5 personal mailbox: it is recorded as SKIP (not_selected)
until OAOS_PERSONAL_MAIL_ADDRESS is stored, and then logs in over IMAP and SMTP.
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

# The wiki's relay is a local bare repository (default ~/data/wiki.git, owner-only). The
# wiki pushes to it after seeding and from the daily backup, so another machine can
# clone or pull it over SSH without touching the working copy. The remote that points at
# the relay is origin, or oaos-relay when the owner already uses origin for something else.
# wiki_relay_remote WIKI_NATIVE RELAY RELAY_NATIVE: prints the remote whose URL is the relay
# (native or Git Bash spelling).
wiki_relay_remote() {
  local wiki_native=$1 relay=$2 relay_native=$3 name url
  while IFS= read -r name; do
    [[ -n $name ]] || continue
    url=$(git -C "$wiki_native" remote get-url "$name" 2>/dev/null || true)
    if [[ -n $url && ( $url == "$relay_native" || $url == "$relay" ) ]]; then
      printf '%s' "$name"
      return 0
    fi
  done < <(git -C "$wiki_native" remote 2>/dev/null)
  return 1
}

ensure_wiki_relay() {
  local wiki=$1 wiki_native relay relay_native remote branch
  wiki_native=$(oaos_native_path "$wiki") || return 3
  relay=$(platform_path relay)
  relay_native=$(oaos_native_path "$relay") || return 3
  if [[ -e $relay ]]; then
    if [[ $(git -C "$relay_native" rev-parse --is-bare-repository 2>/dev/null) != true ]]; then
      warn "Wiki relay path $relay exists but is not a bare git repository; preserving it."
      return 3
    fi
  else
    git init -q --bare -b main "$relay_native"
    info "Created the wiki relay (bare repository) at $relay."
  fi
  [[ $OAOS_PLATFORM == windows-gitbash ]] || chmod 700 "$relay"
  if ! remote=$(wiki_relay_remote "$wiki_native" "$relay" "$relay_native"); then
    if git -C "$wiki_native" remote get-url origin >/dev/null 2>&1; then
      remote=oaos-relay
      warn "Wiki origin already points elsewhere; the relay was added as remote '$remote'."
    else
      remote=origin
    fi
    git -C "$wiki_native" remote add "$remote" "$relay_native"
  fi
  branch=$(git -C "$wiki_native" symbolic-ref --short -q HEAD) || { warn 'Wiki HEAD is detached; check out a branch, then rerun --stage wiki.'; return 3; }
  if ! git -C "$wiki_native" push -q -u "$remote" "$branch" 2>/dev/null; then
    warn "Pushing the wiki to its relay ($remote) was rejected; reconcile $relay with the wiki, then rerun --stage wiki."
    return 3
  fi
  git -C "$relay_native" symbolic-ref HEAD "refs/heads/$branch"
}

do_wiki() {
  local wiki wiki_native relay relay_native
  wiki=$(platform_path wiki)
  [[ -n $wiki ]] || return 3
  wiki_native=$(oaos_native_path "$wiki") || return 3
  if [[ -e $wiki ]]; then
    [[ -d $wiki/.git ]] || { warn 'Wiki path already exists but is not a git repository; preserving it.'; return 3; }
    git -C "$wiki_native" log -1 --format=%h >/dev/null 2>&1 || { warn 'Existing wiki has no commit; preserving it.'; return 3; }
    install_wiki_hook "$wiki"
    ensure_wiki_relay "$wiki"
    return
  fi
  mkdir -p "$(dirname "$wiki")"
  relay=$(platform_path relay)
  relay_native=$(oaos_native_path "$relay") || return 3
  # A missing wiki whose relay still holds history is restored from the relay, not reseeded.
  if [[ $(git -C "$relay_native" rev-parse --is-bare-repository 2>/dev/null) == true ]] &&
     git -C "$relay_native" rev-parse -q --verify HEAD >/dev/null 2>&1; then
    git clone -q "$relay_native" "$wiki_native"
    info "Restored the wiki from its relay $relay."
    install_wiki_hook "$wiki"
    ensure_wiki_relay "$wiki"
    return
  fi
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
  ensure_wiki_relay "$wiki"
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
  info 'Credential typing stays vault-only (upstream default). Owner opt-in: bash editions/personal/computer-use-policy.sh enable — see docs/computer-use-credential-typing.md.'
}

# install_personal_cron_script CANDIDATE SCRIPT NAME [LEGACY]: an existing script equal to the
# candidate is kept, one equal to the LEGACY file is upgraded, anything else is preserved.
install_personal_cron_script() {
  local candidate=$1 script=$2 name=$3 legacy=${4:-}
  if [[ -e $script || -L $script ]]; then
    if cmp -s "$candidate" "$script"; then
      rm -f -- "$candidate"
      return 0
    fi
    if [[ -n $legacy && ! -L $script ]] && cmp -s "$legacy" "$script"; then
      info "Upgrading the $name script (adds the wiki relay push)."
      chmod 700 "$candidate" || { rm -f -- "$candidate"; return 1; }
      mv -f -- "$candidate" "$script" || { rm -f -- "$candidate"; return 1; }
      return 0
    fi
    rm -f -- "$candidate"
    warn "Existing script for $name needs review; preserving it."
    return 3
  fi
  chmod 700 "$candidate" || { rm -f -- "$candidate"; return 1; }
  mv -- "$candidate" "$script" || { rm -f -- "$candidate"; return 1; }
}

# The daily backup as generated by earlier releases (archive only); a script that still
# matches it is upgraded in place to write_backup_script.
write_backup_script_base() {
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

write_backup_script() {
  local wiki
  wiki=$(platform_path wiki)
  write_backup_script_base
  printf '# OAOS wiki relay push: keep the bare relay up to date with the wiki.\n'
  printf 'wiki_git=%q\nrelay_git=%q\n' "$(oaos_native_path "$wiki")" "$(oaos_native_path "$(platform_path relay)")"
  cat <<'EOF'
branch=$(git -C "$wiki_git" symbolic-ref --short HEAD)
git -C "$wiki_git" push -q "$relay_git" "HEAD:refs/heads/$branch"
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
  local scripts bin jobs script script_tmp legacy_tmp code=0
  need_hermes || return 3
  scripts=$(platform_path scripts)
  bin=$(platform_path bin)
  mkdir -p "$scripts"
  private_dir "$bin"
  private_dir "$(platform_path backups)"
  jobs=$(hermes cron list --all 2>/dev/null) || return 3
  # Daily backup: a no-agent Hermes cron script (runs while the gateway is up). It also
  # pushes the wiki to its relay; a script left by an earlier release is upgraded in place.
  script="$scripts/oaos-daily-backup.sh"
  script_tmp=$(mktemp "$script.tmp.XXXXXX")
  legacy_tmp=$(mktemp "$script.legacy.XXXXXX")
  write_backup_script > "$script_tmp" && write_backup_script_base > "$legacy_tmp" ||
    { rm -f -- "$script_tmp" "$legacy_tmp"; return 1; }
  install_personal_cron_script "$script_tmp" "$script" oaos-daily-backup "$legacy_tmp" || code=$?
  rm -f -- "$legacy_tmp"
  if ! grep -Fq oaos-daily-backup <<< "$jobs"; then
    ((code == 0)) || return "$code"
    hermes cron create --name oaos-daily-backup --script "$(basename "$script")" --no-agent --deliver local '0 3 * * *' >/dev/null
  elif ((code != 0)); then
    warn 'The existing oaos-daily-backup script was changed by the owner; it may not push the wiki relay (verify checks the relay).'
  fi
  code=0
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

# --- G5 (optional): personal mailbox for Himalaya ------------------------------------------
# Selected by storing OAOS_PERSONAL_MAIL_ADDRESS; the app password is the secret
# OAOS_PERSONAL_MAIL_APP_PASSWORD. Exit 4 = not selected (recorded as SKIP).
mail_password_command() { printf '%s/oaos-mail-password' "$(platform_path bin)"; }

# The password command Himalaya runs (auth.cmd). It reads the Hermes .env at call time,
# so the app password is never copied into the Himalaya config. The key name is kept
# apart from the sed expression so the secret-hygiene scan of ~/.oaos sees no assignment.
write_mail_password_command() {
  printf '#!/usr/bin/env bash\n# OAOS Personal G5 password command for Himalaya (auth.cmd); managed by editions/personal/install.sh.\n'
  printf '# Prints the app password from the Hermes .env. Owner-only; never run it where output is logged.\nset -euo pipefail\n'
  printf 'env_file=%q\nkey=OAOS_PERSONAL_MAIL_APP_PASSWORD\n' "$(oaos_hermes_env_file)"
  printf '%s\n' 'sed -n "s/^${key}=//p" "$env_file" | tail -n 1'
}

personal_himalaya_marker='# OAOS Personal managed Himalaya account'
write_himalaya_config() {
  local address=$1 imap=$2 smtp=$3 command=$4
  printf '%s (the app password comes from the password command, never this file).\n' "$personal_himalaya_marker"
  printf '[accounts.oaos-personal]\ndefault = true\nemail = "%s"\n' "$address"
  printf 'backend.type = "imap"\nbackend.host = "%s"\nbackend.port = 993\nbackend.encryption.type = "tls"\n' "$imap"
  printf 'backend.login = "%s"\nbackend.auth.type = "password"\nbackend.auth.cmd = "%s"\n' "$address" "$command"
  printf 'message.send.backend.type = "smtp"\nmessage.send.backend.host = "%s"\nmessage.send.backend.port = 587\n' "$smtp"
  printf 'message.send.backend.encryption.type = "start-tls"\nmessage.send.backend.login = "%s"\n' "$address"
  printf 'message.send.backend.auth.type = "password"\nmessage.send.backend.auth.cmd = "%s"\n' "$command"
}

# Installs a generated owner-only file: replaced when absent or OAOS-managed (MARKER in
# its first two lines); otherwise preserved. Exit 0 written or unchanged, 4 preserved.
install_managed_file() {
  local candidate=$1 target=$2 marker=$3 mode=$4
  if [[ -e $target || -L $target ]]; then
    if cmp -s "$candidate" "$target"; then rm -f -- "$candidate"; return 0; fi
    case $(sed -n 1,2p "$target" 2>/dev/null) in
      *"$marker"*) ;;
      *) rm -f -- "$candidate"; return 4 ;;
    esac
  fi
  if [[ $OAOS_PLATFORM != windows-gitbash ]]; then chmod "$mode" "$candidate" || { rm -f -- "$candidate"; return 1; }; fi
  mv -f -- "$candidate" "$target" || { rm -f -- "$candidate"; return 1; }
}

do_mail() {
  local address imap smtp password result config command tmp code=0 rerun
  address=$(oaos_gate_value OAOS_PERSONAL_MAIL_ADDRESS || true)
  if [[ -z $address ]]; then
    info "G5 mail is optional and not selected. To add it, store the mailbox address with '$(secret_hint OAOS_PERSONAL_MAIL_ADDRESS)' and rerun --stage mail."
    return 4
  fi
  oaos_mail_valid_address "$address" || {
    warn "G5: use a plain address (letters, digits, dot, underscore, plus, hyphen); store it again with '$(secret_hint OAOS_PERSONAL_MAIL_ADDRESS)'."; return 3; }
  imap=$(oaos_gate_value OAOS_PERSONAL_MAIL_IMAP_HOST || oaos_mail_default_host imap "$address" || true)
  smtp=$(oaos_gate_value OAOS_PERSONAL_MAIL_SMTP_HOST || oaos_mail_default_host smtp "$address" || true)
  [[ -n $imap && -n $smtp ]] || {
    warn "G5: for a non-Gmail mailbox store the provider's hosts with '$(secret_hint OAOS_PERSONAL_MAIL_IMAP_HOST)' and '$(secret_hint OAOS_PERSONAL_MAIL_SMTP_HOST)' (Gmail defaults to imap.gmail.com / smtp.gmail.com)."; return 3; }
  { oaos_mail_valid_host "$imap" && oaos_mail_valid_host "$smtp"; } || { warn 'G5: invalid IMAP or SMTP host name.'; return 3; }
  oaos_env_put OAOS_PERSONAL_MAIL_IMAP_HOST "$imap" || return 1
  oaos_env_put OAOS_PERSONAL_MAIL_SMTP_HOST "$smtp" || return 1
  password=$(oaos_secret_value OAOS_PERSONAL_MAIL_APP_PASSWORD || true)
  [[ -n $password ]] || {
    warn "G5: turn on 2-Step Verification for the mailbox, create an app password at https://myaccount.google.com/apppasswords (it lets the agent read and send this mailbox without your Google password), and store it without spaces with '$(secret_hint OAOS_PERSONAL_MAIL_APP_PASSWORD)'."; return 3; }
  if ! oaos_mail_valid_app_password "$password"; then
    password=''
    warn "G5: the app password must be one word without spaces; store it again with '$(secret_hint OAOS_PERSONAL_MAIL_APP_PASSWORD)'."
    return 3
  fi
  password=''
  result=$(oaos_mail_login imap "$address" "$imap" OAOS_PERSONAL_MAIL_APP_PASSWORD) || {
    warn "G5: IMAP login to $imap failed ($result). Check the address, IMAP access and the app password (https://myaccount.google.com/apppasswords), store it again with '$(secret_hint OAOS_PERSONAL_MAIL_APP_PASSWORD)', and rerun --stage mail."; return 3; }
  result=$(oaos_mail_login smtp "$address" "$smtp" OAOS_PERSONAL_MAIL_APP_PASSWORD) || {
    warn "G5: SMTP login to $smtp:587 failed ($result). Check the SMTP host and the app password, then rerun --stage mail."; return 3; }
  info 'G5: IMAP and SMTP logins succeeded.'
  rerun="rerun 'bash editions/personal/install.sh --stage mail --recheck'"
  config=$(platform_path himalaya) || code=$?
  if ((code == 4)); then
    warn "G5 MANUAL: on Windows configure Himalaya yourself (https://pimalaya.org/himalaya/) with a password command that reads OAOS_PERSONAL_MAIL_APP_PASSWORD from the Hermes .env, never a plain-text password; then confirm with 'bash bootstrap/verify/personal-verify.sh --confirm mail'."
    return 0
  fi
  ((code == 0)) || return 1
  if ! have_cmd himalaya; then
    warn "G5 MANUAL: install Himalaya 1.x from https://pimalaya.org/himalaya/ (verify the release before installing), then $rerun."
    return 0
  fi
  if ! oaos_himalaya_version_ok; then
    warn "G5 MANUAL: OAOS configures Himalaya 1.x only; install a verified 1.x release from https://pimalaya.org/himalaya/ and $rerun, or configure this version yourself."
    return 0
  fi
  command=$(mail_password_command)
  if [[ ! $command =~ ^[A-Za-z0-9._/@+-]+$ ]]; then
    warn "G5 MANUAL: the password command path $command needs quoting; configure Himalaya yourself with that command."
    return 0
  fi
  private_dir "$(platform_path bin)"
  tmp=$(mktemp "$command.tmp.XXXXXX")
  write_mail_password_command > "$tmp" || { rm -f -- "$tmp"; return 1; }
  code=0
  install_managed_file "$tmp" "$command" 'OAOS Personal G5 password command' 700 || code=$?
  if ((code == 4)); then warn "G5: $command was changed by the owner; preserving it."
  elif ((code != 0)); then return 1; fi
  private_dir "$(dirname "$config")"
  tmp=$(mktemp "$config.tmp.XXXXXX")
  write_himalaya_config "$address" "$imap" "$smtp" "$command" > "$tmp" || { rm -f -- "$tmp"; return 1; }
  code=0
  install_managed_file "$tmp" "$config" "$personal_himalaya_marker" 600 || code=$?
  if ((code == 4)); then
    warn "G5 MANUAL: $config is your own Himalaya config and was preserved. Add an account for $address whose auth.cmd is $command, then confirm with 'bash bootstrap/verify/personal-verify.sh --confirm mail'."
    return 0
  fi
  ((code == 0)) || return 1
  if ! oaos_himalaya_account_check "$config" oaos-personal; then
    warn 'G5: Himalaya could not read the mailbox with the generated account oaos-personal; review the config, then rerun --stage mail.'
    return 3
  fi
  info 'G5: Himalaya account oaos-personal reads the mailbox.'
}

# personal-verify.sh exit: 0 all PASS; 2 MANUAL pending, no FAIL; 1 FAIL; 3 blocked.
do_verify() { bash "$repo_root/bootstrap/verify/personal-verify.sh"; }

# Stage dispatch is deliberately explicit: no user-controlled command name is evaluated.
run_stage() {
  case $1 in
    prep) do_prep ;; hermes) do_hermes ;; llm) do_llm ;;
    telegram) do_telegram ;; gateway) do_gateway ;; wiki) do_wiki ;;
    harness) do_harness ;; cron) do_cron ;; mail) do_mail ;; verify) do_verify ;;
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
    4) stage_mark "$stage" SKIP 'not_selected: optional G5 mail; store OAOS_PERSONAL_MAIL_ADDRESS to select it'; info "$stage SKIP (not selected)." ;;
    3) stage_mark "$stage" blocked 'Needs a gate, prerequisite, or permission'; warn "$stage blocked."; note_result 3 ;;
    *) stage_mark "$stage" failed 'Command failed; review the install log'; warn "$stage failed."; note_result 1 ;;
  esac
done
show_table
exit "$result_code"
