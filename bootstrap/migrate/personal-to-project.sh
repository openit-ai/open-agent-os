#!/usr/bin/env bash
set -Eeuo pipefail

source_host=''
source_root=''
source_hermes_home=''
wiki_remote=''
dry_run=0
include_config=0
usage() {
  cat <<'HELP'
Usage: bash bootstrap/migrate/personal-to-project.sh (--source user@host | --source-root DIR)
         [--source-hermes-home PATH] [--wiki-remote URL] [--include-config] [--dry-run] [--help]

Run on the Project server as the account that runs Hermes. Copies the Personal knowledge
and harness into this account:
  ~/data/wiki                 -> ~/data/wiki                 (replaced; old copy kept as .backup-<ts>)
  <hermes>/SOUL.md, AGENTS.md -> $HERMES_HOME/               (replaced; AGENTS.md only if present)
  <hermes>/memories/          -> $HERMES_HOME/memories/      (replaced)
  <hermes>/skills/            -> $HERMES_HOME/skills/        (merged; existing files win)
  <hermes>/scripts/           -> $HERMES_HOME/scripts/       (merged; oaos-* edition scripts excluded)
  <hermes>/config.yaml        -> $HERMES_HOME/config.yaml    (only with --include-config)
--source-root DIR    read from a local directory laid out like the Personal HOME
                     (for example an extracted backup) instead of SSH.
--source-hermes-home Personal HERMES_HOME (default: the source's $HERMES_HOME or ~/.hermes).
--wiki-remote URL    set the migrated wiki's origin; otherwise every remote copied from the
                     Personal wiki is removed so pushes cannot go back there. That includes
                     the Personal relay (origin or oaos-relay -> ~/data/wiki.git on the
                     Personal host), which is never copied: the wiki carries its full history.
The Hermes gateway is stopped during the swap and started again if it was running.
Hermes .env is never copied; re-enter credentials on the Project server.
Exit: 0 migrated (or planned); 3 source missing or tool unavailable; 1 error.
HELP
}
while (($#)); do
  case $1 in
    --source) (($# >= 2)) || { usage >&2; exit 1; }; source_host=$2; shift 2 ;;
    --source-root) (($# >= 2)) || { usage >&2; exit 1; }; source_root=$2; shift 2 ;;
    --source-hermes-home) (($# >= 2)) || { usage >&2; exit 1; }; source_hermes_home=$2; shift 2 ;;
    --wiki-remote) (($# >= 2)) || { usage >&2; exit 1; }; wiki_remote=$2; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    --include-config) include_config=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
done
[[ -n ${HOME:-} ]] || { printf 'HOME is required.\n' >&2; exit 1; }
if [[ -n $source_host && -n $source_root ]] || [[ -z $source_host && -z $source_root ]]; then usage >&2; exit 1; fi
if [[ -n $source_host ]]; then
  [[ $source_host =~ ^[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || { usage >&2; exit 1; }
fi
safe_path='^/[A-Za-z0-9._/@+-]+$'
ssh_opts=(-o BatchMode=yes)

# Remote paths are resolved to absolute, character-checked paths before use.
remote() { ssh "${ssh_opts[@]}" -- "$source_host" "$1"; }
if [[ -n $source_root ]]; then
  [[ -d $source_root ]] || { printf 'Source root is not a directory: %s\n' "$source_root" >&2; exit 3; }
  source_root=$(cd "$source_root" && pwd)
  src_home=$source_root
  src_hermes=${source_hermes_home:-$src_home/.hermes}
  [[ $src_hermes == /* ]] || src_hermes="$src_home/${src_hermes#\~/}"
else
  command -v ssh >/dev/null || { printf 'ssh is required.\n' >&2; exit 3; }
  src_home=$(remote 'printf %s "$HOME"') || { printf 'Cannot reach %s over SSH (BatchMode).\n' "$source_host" >&2; exit 3; }
  if [[ -n $source_hermes_home ]]; then
    src_hermes=$source_hermes_home
    [[ $src_hermes == /* ]] || src_hermes="$src_home/${src_hermes#\~/}"
  else
    src_hermes=$(remote 'printf %s "${HERMES_HOME:-$HOME/.hermes}"') || exit 3
  fi
fi
[[ $src_home =~ $safe_path && $src_hermes =~ $safe_path ]] ||
  { printf 'Source paths must be absolute and contain only [A-Za-z0-9._/@+-].\n' >&2; exit 1; }
dst_hermes=${HERMES_HOME:-$HOME/.hermes}

# kind: dir-replace | dir-merge | file ; need: required | optional
items=(
  "dir-replace|required|$src_home/data/wiki|$HOME/data/wiki"
  "file|required|$src_hermes/SOUL.md|$dst_hermes/SOUL.md"
  "dir-replace|required|$src_hermes/memories|$dst_hermes/memories"
  "file|optional|$src_hermes/AGENTS.md|$dst_hermes/AGENTS.md"
  "dir-merge|optional|$src_hermes/skills|$dst_hermes/skills"
  "dir-merge|optional|$src_hermes/scripts|$dst_hermes/scripts"
)
((include_config)) && items+=("file|required|$src_hermes/config.yaml|$dst_hermes/config.yaml")

src_label() { if [[ -n $source_host ]]; then printf '%s:%s' "$source_host" "$1"; else printf '%s' "$1"; fi; }
src_test() {
  if [[ -n $source_root ]]; then test "-$1" "$2"
  else remote "test -$1 '$2'"; fi
}
src_rsync() {
  local from=$1 to=$2; shift 2
  if [[ -n $source_root ]]; then rsync -a "$@" -- "$from" "$to"
  else rsync -a -e "ssh ${ssh_opts[*]}" "$@" -- "$source_host:$from" "$to"; fi
}

printf 'Source: %s (Hermes home %s)\n' "$(src_label "$src_home")" "$src_hermes"
printf 'Target: %s (Hermes home %s)\n' "$HOME" "$dst_hermes"
printf 'Hermes .env is excluded. Re-enter all Project credentials after migration.\n'
present=()
for item in "${items[@]}"; do
  IFS='|' read -r kind need from to <<< "$item"
  test_flag=f
  [[ $kind == file ]] || test_flag=d
  if ((dry_run)); then
    printf 'PLAN %-11s %s -> %s\n' "$kind" "$(src_label "$from")" "$to"
    continue
  fi
  if src_test "$test_flag" "$from"; then present+=("$item"); continue; fi
  if [[ $need == required ]]; then
    printf 'Source missing or inaccessible: %s\n' "$(src_label "$from")" >&2
    exit 3
  fi
  printf 'Optional source absent, skipped: %s\n' "$(src_label "$from")"
done
if ((dry_run)); then
  printf 'PLAN gateway stop during swap; wiki remotes (Personal relay included) removed%s\n' "${wiki_remote:+, origin set to the given URL}"
  printf 'PLAN Personal relay ~/data/wiki.git stays on the Personal host (not copied)\n'
  exit 0
fi
command -v rsync >/dev/null || { printf 'rsync is required.\n' >&2; exit 3; }

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
staged=()
cleanup() { local s; for s in ${staged[@]+"${staged[@]}"}; do rm -rf -- "${s##*|}"; done; }
trap cleanup EXIT

# 1. Transfer everything into staging paths; no target changes until all succeed.
for item in ${present[@]+"${present[@]}"}; do
  IFS='|' read -r kind _ from to <<< "$item"
  mkdir -p "$(dirname "$to")"
  staging="${to}.incoming-${timestamp}-$$"
  [[ ! -e $staging ]] || { printf 'Staging path exists: %s\n' "$staging" >&2; exit 1; }
  staged+=("$item|$staging")
  if [[ $kind == file ]]; then
    src_rsync "$from" "$staging" || { printf 'Transfer failed for %s; targets unchanged.\n' "$from" >&2; exit 1; }
  else
    mkdir -p "$staging"
    exclude=()
    [[ $to != "$dst_hermes/scripts" ]] || exclude=(--exclude 'oaos-*')
    # Trailing slashes copy the directory's contents, so data/wiki never nests as wiki/wiki.
    src_rsync "$from/" "$staging/" ${exclude[@]+"${exclude[@]}"} || { printf 'Transfer failed for %s; targets unchanged.\n' "$from" >&2; exit 1; }
  fi
done

# 2. Stop the gateway so it does not read half-swapped files.
gateway_was_running=0
gateway_running() {
  local status
  status=$(hermes gateway status 2>/dev/null) || return 1
  printf '%s\n' "$status" | grep -Ei '(^|[[:space:]:])(running|active)([[:space:]]|$)' >/dev/null &&
    ! printf '%s\n' "$status" | grep -Ei 'not running|inactive|stopped' >/dev/null
}
if command -v hermes >/dev/null 2>&1 && gateway_running; then
  hermes gateway stop >/dev/null 2>&1 || { printf 'Could not stop the Hermes gateway; nothing was swapped.\n' >&2; exit 1; }
  gateway_was_running=1
  printf 'Gateway stopped for the swap.\n'
fi
restart_gateway() {
  if ((gateway_was_running)); then
    if hermes gateway start >/dev/null 2>&1; then printf 'Gateway started again.\n'
    else printf 'Gateway did not start; run hermes gateway start.\n' >&2; fi
    gateway_was_running=0
  fi
}
trap 'restart_gateway; cleanup' EXIT

# 3. Swap: replaced targets keep a .backup-<ts>; merged directories keep a full copy first.
for entry in ${staged[@]+"${staged[@]}"}; do
  IFS='|' read -r kind _ _ to staging <<< "$entry"
  backup="${to}.backup-${timestamp}"
  if [[ $kind == dir-merge ]]; then
    if [[ -e $to ]]; then
      [[ ! -e $backup ]] || { printf 'Backup path exists: %s\n' "$backup" >&2; exit 1; }
      cp -a -- "$to" "$backup"
      printf 'Preserved existing %s as %s\n' "$to" "$backup"
    fi
    mkdir -p "$to"
    rsync -a --ignore-existing -- "$staging/" "$to/"
    rm -rf -- "$staging"
  else
    if [[ -e $to || -L $to ]]; then
      [[ ! -e $backup ]] || { printf 'Backup path exists: %s\n' "$backup" >&2; exit 1; }
      mv -- "$to" "$backup"
      printf 'Preserved existing %s as %s\n' "$to" "$backup"
    fi
    mv -- "$staging" "$to"
  fi
  printf 'MIGRATED %s\n' "$to"
done
staged=()

# 4. The Personal wiki's remotes point at its relay (~/data/wiki.git, a path on the Personal
#    host) or at the owner's own remotes; never push Project data there.
wiki="$HOME/data/wiki"
if [[ -d $wiki/.git ]]; then
  while IFS= read -r name; do
    [[ -n $name ]] || continue
    url=$(git -C "$wiki" remote get-url "$name" 2>/dev/null || true)
    git -C "$wiki" remote remove "$name"
    case $url in
      */data/wiki.git|*\\data\\wiki.git)
        printf 'Removed wiki remote %s (the Personal relay ~/data/wiki.git; it stays on the Personal host).\n' "$name" ;;
      *) printf 'Removed wiki remote %s (copied from the Personal wiki).\n' "$name" ;;
    esac
  done < <(git -C "$wiki" remote)
  if [[ -n $wiki_remote ]]; then
    git -C "$wiki" remote add origin "$wiki_remote"
    printf 'Wiki origin set to the given URL.\n'
  fi
  git -C "$wiki" log -1 --format='Wiki latest commit: %h %s' || true
  printf 'Wiki files: '
  find "$wiki" -path "$wiki/.git" -prune -o -type f -print | wc -l
fi
restart_gateway
