#!/usr/bin/env bash
# OAOS Project daily backup. Installed by editions/project/install.sh (cron stage) as
# $HERMES_HOME/scripts/oaos-daily-backup.sh and run by the Hermes cron job oaos-daily-backup.
# The installer replaces this managed copy on upgrade; keep local changes elsewhere.
#
# One archive per run: ~/.oaos/backups/oaos-project-YYYYmmdd-HHMMSS.tar (directory 700,
# archive 600, newest 7 kept). Inside:
#   manifest.txt      components captured
#   home.tar.gz       Hermes home, ~/data/wiki, ~/oaos/stack (stack .env holds DB secrets)
#   mattermost.dump   pg_dump -Fc of the mattermost database   (when the stack is installed)
#   outline.dump      pg_dump -Fc of the outline database      (when the stack is installed)
#   system.tar.gz     /etc/oaos, Mattermost config.json, /opt/mattermost/data, /var/lib/outline/data
# Database and system parts need non-interactive sudo (the Project installer requires it).
# Restore: bash editions/project/restore.sh --dry-run ARCHIVE, then --yes ARCHIVE.
set -Eeuo pipefail
umask 077

hermes_home=${HERMES_HOME:-$HOME/.hermes}
backup_dir=${OAOS_BACKUP_DIR:-$HOME/.oaos/backups}
keep=${OAOS_BACKUP_KEEP:-7}
[[ $keep =~ ^[1-9][0-9]*$ ]] || keep=7
system_root=/
if [[ ${OAOS_TEST_MODE:-0} == 1 && -n ${OAOS_TEST_SYSTEM_ROOT:-} ]]; then system_root=$OAOS_TEST_SYSTEM_ROOT; fi
as_root() { if ((EUID == 0)); then "$@"; else sudo -n "$@"; fi; }
die() { printf 'oaos-daily-backup: %s\n' "$1" >&2; exit 1; }

[[ -d $hermes_home ]] || die 'Hermes home is absent.'
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
stamp=$(date +%Y%m%d-%H%M%S)
work=$(mktemp -d "$backup_dir/.oaos-project-$stamp.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
manifest="$work/manifest.txt"
printf 'oaos-project-backup 1\ncreated %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$manifest"

# 1. User data. The Hermes home is recorded relative to its parent so restore can put it back.
home_items=()
for item in data/wiki oaos/stack; do [[ -e $HOME/$item ]] && home_items+=("$item"); done
tar -czf "$work/home.tar.gz" -C "$(dirname "$hermes_home")" "$(basename "$hermes_home")" \
  ${home_items[@]+-C "$HOME" "${home_items[@]}"}
printf 'home %s %s\n' "$(basename "$hermes_home")" "${home_items[*]}" >> "$manifest"

# 2–3. Databases and service data exist once the stack stage has run.
if [[ -f $HOME/oaos/stack/.env ]]; then
  for db in mattermost outline; do
    as_root runuser -u postgres -- pg_dump -Fc "$db" > "$work/$db.dump" || die "pg_dump $db failed."
    [[ -s $work/$db.dump ]] || die "pg_dump $db produced an empty dump."
    printf 'database %s\n' "$db" >> "$manifest"
  done
  system_items=()
  for item in etc/oaos opt/mattermost/config/config.json opt/mattermost/data var/lib/outline/data; do
    if as_root test -e "$system_root/$item"; then system_items+=("$item"); fi
  done
  for item in etc/oaos opt/mattermost/data var/lib/outline/data; do
    [[ " ${system_items[*]} " == *" $item "* ]] || die "/$item is absent; the backup would be incomplete."
  done
  as_root tar -czf - -C "$system_root" "${system_items[@]}" > "$work/system.tar.gz" || die 'System file archive failed.'
  printf 'system %s\n' "${system_items[*]}" >> "$manifest"
fi

target="$backup_dir/oaos-project-$stamp.tar"
tmp="$work.tar.tmp"
trap 'rm -rf -- "$work" "$tmp"' EXIT
tar -cf "$tmp" -C "$work" .
chmod 600 "$tmp"
mv -- "$tmp" "$target"

# Rotation: keep the newest $keep archives.
count=0
while IFS= read -r old; do
  count=$((count + 1))
  if ((count > keep)); then rm -f -- "$old"; fi
done < <(find "$backup_dir" -maxdepth 1 -type f -name 'oaos-project-*.tar' -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2-)
printf '%s\n' "$target"
