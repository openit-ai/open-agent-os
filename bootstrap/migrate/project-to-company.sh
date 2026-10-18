#!/usr/bin/env bash
set -Eeuo pipefail

source_host=''
dry_run=0
include_config=0
usage() {
  cat <<'HELP'
Usage: bash bootstrap/migrate/project-to-company.sh --source user@host [--dry-run] [--include-config] [--help]
Run on the Company server to copy the Project wiki, memory and settings.
Hermes .env and credentials are never copied; re-register them at the Company gate.
Existing targets are preserved as .backup-<timestamp> before replacement, and the Project source is left unchanged.
Reverse (Company->Project) conversion is out of scope.
HELP
}
while (($#)); do
  case $1 in
    --source) (($# >= 2)) || { usage >&2; exit 1; }; source_host=$2; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    --include-config) include_config=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
done
[[ $source_host =~ ^[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || { usage >&2; exit 1; }
[[ -n ${HOME:-} ]] || { printf 'HOME is required.\n' >&2; exit 1; }

paths=(data/wiki .hermes/SOUL.md .hermes/memories/USER.md .hermes/memories/MEMORY.md)
((include_config)) && paths+=(.hermes/config.yaml)
printf 'Source: %s\n' "$source_host"
printf 'Hermes .env and credentials are excluded. Re-enter all Company credentials after migration.\n'
printf 'Wiki note: the Company gate indexes only public wiki documents with a verified source ACL and copies a personal wiki after owner confirmation.\n'
for relative in "${paths[@]}"; do
  printf '%s: %s:~/%s -> %s/%s\n' "$([[ $dry_run == 1 ]] && printf PLAN || printf COPY)" "$source_host" "$relative" "$HOME" "$relative"
done
printf 'Reverse (Company->Project) conversion is out of scope.\n'
((dry_run)) && exit 0
command -v rsync >/dev/null || { printf 'rsync is required.\n' >&2; exit 3; }
command -v ssh >/dev/null || { printf 'ssh is required.\n' >&2; exit 3; }
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
for relative in "${paths[@]}"; do
  target="$HOME/$relative"
  if ! ssh -o BatchMode=yes -- "$source_host" "test -e \"\$HOME/$relative\""; then
    printf 'Source missing or inaccessible: %s\n' "$relative" >&2
    exit 3
  fi
  mkdir -p "$(dirname "$target")"
  staging="${target}.incoming-${timestamp}-$$"
  [[ ! -e $staging ]] || { printf 'Staging path exists: %s\n' "$staging" >&2; exit 1; }
  if ! rsync -a -e 'ssh -o BatchMode=yes' -- "$source_host:~/$relative" "$staging"; then
    printf 'Transfer failed for %s; existing target was preserved.\n' "$relative" >&2
    exit 1
  fi
  if [[ -e $target ]]; then
    backup="${target}.backup-${timestamp}"
    [[ ! -e $backup ]] || { printf 'Backup path exists: %s\n' "$backup" >&2; exit 1; }
    mv -- "$target" "$backup"
    printf 'Preserved existing %s at %s\n' "$relative" "$backup"
  fi
  mv -- "$staging" "$target"
done
if [[ -d $HOME/data/wiki/.git ]]; then
  git -C "$HOME/data/wiki" log -1 --format='Wiki latest commit: %h %s' 2>/dev/null || true
  printf 'Wiki files: '
  find "$HOME/data/wiki" -path "$HOME/data/wiki/.git" -prune -o -type f -print | wc -l
fi
printf 'Recovery point: migration %s. Replaced targets keep a .backup-%s copy.\n' "$timestamp" "$timestamp"
printf 'Next: re-index the ACL-checked public wiki, confirm personal wiki ownership, and re-enter credentials at the Company gate.\n'
