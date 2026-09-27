#!/usr/bin/env bash
# Installed by C02. EnvironmentFile supplies only non-secret paths and PGDATABASE.
set -Eeuo pipefail
umask 077
[[ ${PGDATABASE:-} && ${OAOS_COMPANY_BACKUP_DIR:-} && ${OAOS_COMPANY_KEY_BACKUP_DIR:-} ]] || exit 1
mkdir -p "$OAOS_COMPANY_BACKUP_DIR" "$OAOS_COMPANY_KEY_BACKUP_DIR"
chmod 700 "$OAOS_COMPANY_BACKUP_DIR" "$OAOS_COMPANY_KEY_BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$OAOS_COMPANY_BACKUP_DIR/company-$stamp.dump"
tmp=$(mktemp "$target.tmp.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
pg_dump --format=custom --file="$tmp" "$PGDATABASE"
test -s "$tmp"
mv -- "$tmp" "$target"
state="${HOME:?}/.oaos-install/company-state.json"
if [[ -f $state ]]; then cp -p -- "$state" "$OAOS_COMPANY_BACKUP_DIR/company-state-$stamp.json"; fi
env_file="$HOME/.config/oaos-company/.env"
if [[ -f $env_file ]]; then cp -p -- "$env_file" "$OAOS_COMPANY_KEY_BACKUP_DIR/company-env-$stamp"; fi
printf '%s\n' "$stamp" > "$OAOS_COMPANY_BACKUP_DIR/last-success"
