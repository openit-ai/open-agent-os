#!/usr/bin/env bash
# Restore an OAOS Project backup made by editions/project/backup.sh.
#   bash editions/project/restore.sh --dry-run ARCHIVE   check every part, print the plan, change nothing
#   bash editions/project/restore.sh --yes ARCHIVE       restore (stops services, then starts them again)
# Exit: 0 ok; 1 invalid archive or failed step; 3 blocked (no non-interactive sudo).
set -Eeuo pipefail
umask 077

usage() {
  cat <<'EOF'
Usage: bash editions/project/restore.sh (--dry-run | --yes) ARCHIVE

--dry-run  extract to a private temporary directory, check the manifest, both tar parts
           and both database dumps (pg_restore --list), and print what would be restored.
--yes      restore: stop the Hermes gateway, Outline and Mattermost; move the current Hermes
           home, wiki and stack directory aside (*.pre-restore-<time>); restore them, the
           system files and both databases (pg_restore --clean --if-exists); start services.
EOF
}

mode=''
archive=''
while (($#)); do
  case $1 in
    --dry-run) mode=dry; shift ;;
    --yes) mode=apply; shift ;;
    --help|-h) usage; exit 0 ;;
    -*) usage >&2; exit 1 ;;
    *) archive=$1; shift ;;
  esac
done
[[ -n $mode && -n $archive ]] || { usage >&2; exit 1; }
[[ -f $archive ]] || { printf 'Archive not found: %s\n' "$archive" >&2; exit 1; }

hermes_home=${HERMES_HOME:-$HOME/.hermes}
system_root=/
if [[ ${OAOS_TEST_MODE:-0} == 1 && -n ${OAOS_TEST_SYSTEM_ROOT:-} ]]; then system_root=$OAOS_TEST_SYSTEM_ROOT; fi
as_root() { if ((EUID == 0)); then "$@"; else sudo -n "$@"; fi; }
fail() { printf 'restore: %s\n' "$1" >&2; exit 1; }

mode_bits=$(stat -c '%a' "$archive" 2>/dev/null || stat -f '%Lp' "$archive")
[[ $mode_bits == 600 ]] || printf 'WARN: archive mode is %s, expected 600.\n' "$mode_bits" >&2

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
tar -xf "$archive" -C "$work" || fail 'archive cannot be read.'
[[ -f $work/manifest.txt ]] && grep -Fx 'oaos-project-backup 1' "$work/manifest.txt" >/dev/null ||
  fail 'manifest.txt is missing or not an OAOS Project backup.'
[[ -s $work/home.tar.gz ]] && tar -tzf "$work/home.tar.gz" >/dev/null || fail 'home.tar.gz is missing or damaged.'
home_line=$(grep '^home ' "$work/manifest.txt" || true)
read -r _ home_leaf home_items <<< "$home_line"
[[ -n $home_leaf ]] || fail 'manifest has no home entry.'
databases=()
while read -r _ db; do databases+=("$db"); done < <(grep '^database ' "$work/manifest.txt" || true)
for db in ${databases[@]+"${databases[@]}"}; do
  [[ $db =~ ^(mattermost|outline)$ ]] || fail "unexpected database entry: $db"
  [[ -s $work/$db.dump ]] || fail "$db.dump is missing."
  pg_restore --list "$work/$db.dump" >/dev/null 2>&1 || fail "$db.dump is not a readable pg_dump archive."
done
system_line=$(grep '^system ' "$work/manifest.txt" || true)
if [[ -n $system_line ]]; then
  [[ -s $work/system.tar.gz ]] && tar -tzf "$work/system.tar.gz" >/dev/null || fail 'system.tar.gz is missing or damaged.'
fi

printf 'Backup: %s\n' "$(sed -n 's/^created //p' "$work/manifest.txt")"
printf 'Would restore Hermes home %s/%s (current copy moved aside)\n' "$(dirname "$hermes_home")" "$home_leaf"
for item in $home_items; do printf 'Would restore %s/%s (current copy moved aside)\n' "$HOME" "$item"; done
for db in ${databases[@]+"${databases[@]}"}; do printf 'Would restore database %s (pg_restore --clean --if-exists)\n' "$db"; done
[[ -z $system_line ]] || printf 'Would restore system files: %s\n' "${system_line#system }"

if [[ $mode == dry ]]; then
  printf 'Dry run: archive is complete and readable; nothing was changed.\n'
  exit 0
fi

if ((EUID != 0)) && ! sudo -n true >/dev/null 2>&1; then
  printf 'BLOCKED: restore needs non-interactive sudo.\n' >&2; exit 3
fi
stamp=$(date +%Y%m%d-%H%M%S)
hermes gateway stop >/dev/null 2>&1 || true
if [[ -n $system_line || ${#databases[@]} -gt 0 ]]; then
  as_root systemctl stop oaos-outline mattermost || fail 'could not stop Outline and Mattermost.'
fi
for path in "$(dirname "$hermes_home")/$home_leaf" $(for item in $home_items; do printf '%s/%s ' "$HOME" "$item"; done); do
  if [[ -e $path ]]; then mv -- "$path" "$path.pre-restore-$stamp"; fi
done
tar -xzf "$work/home.tar.gz" -C "$(dirname "$hermes_home")" "$home_leaf"
for item in $home_items; do tar -xzf "$work/home.tar.gz" -C "$HOME" "$item"; done
if [[ -n $system_line ]]; then
  as_root tar -xzpf - -C "$system_root" < "$work/system.tar.gz" || fail 'system files could not be restored.'
fi
for db in ${databases[@]+"${databases[@]}"}; do
  as_root runuser -u postgres -- pg_restore --clean --if-exists --no-owner --role="$db" -d "$db" < "$work/$db.dump" ||
    fail "database $db could not be restored; services stay stopped for review."
done
if [[ -n $system_line || ${#databases[@]} -gt 0 ]]; then
  as_root systemctl start mattermost oaos-outline || fail 'Mattermost or Outline did not start.'
fi
hermes gateway start >/dev/null 2>&1 || printf 'WARN: start the Hermes gateway with: hermes gateway start\n' >&2
printf 'Restore complete. Previous copies end with .pre-restore-%s. Run bash bootstrap/verify/project-verify.sh.\n' "$stamp"
