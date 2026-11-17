#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Installed by C02 as the oaos-company-backup.service ExecStart. The
# EnvironmentFile supplies only non-secret paths, PGDATABASE and PGPORT.
#
# FORCE RLS makes a dump fail for any role that does not bypass RLS, so the dump
# runs as the dedicated read-only job role oaos_company_backup (BYPASSRLS,
# pg_read_all_data, no write privilege; db-jobs.sql) over loopback TCP with the
# Company account's own 0600 libpq password file. No root, postgres account or
# privilege change is involved, so the unit runs with NoNewPrivileges.
#
# Retention: 7 newest daily dumps plus the newest dump of each of the 4 most
# recent older ISO weeks. The matching state and EnvironmentFile copies follow
# the dumps. Directories are 0700 and files 0600.
set -Eeuo pipefail
umask 077
[[ ${PGDATABASE:-} =~ ^[A-Za-z_][A-Za-z0-9_]*$ && ${OAOS_COMPANY_BACKUP_DIR:-} && ${OAOS_COMPANY_KEY_BACKUP_DIR:-} ]] || exit 1
port=${PGPORT:-5432}
passfile="${HOME:?}/.config/oaos-company/db-jobs.pgpass"
[[ $port =~ ^[0-9]{1,5}$ && -f $passfile && ! -L $passfile ]] || exit 1
[[ $(stat -c %a "$passfile") == 600 && $(stat -c %u "$passfile") == "$(id -u)" ]] || exit 1

mkdir -p "$OAOS_COMPANY_BACKUP_DIR" "$OAOS_COMPANY_KEY_BACKUP_DIR"
chmod 700 "$OAOS_COMPANY_BACKUP_DIR" "$OAOS_COMPANY_KEY_BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$OAOS_COMPANY_BACKUP_DIR/company-$stamp.dump"
tmp=$(mktemp "$target.tmp.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
env -u PGOPTIONS -u PGSERVICE -u PGSERVICEFILE -u PGPASSWORD \
  PGHOST=127.0.0.1 PGHOSTADDR=127.0.0.1 PGPORT="$port" PGUSER=oaos_company_backup \
  PGPASSFILE="$passfile" PGCONNECT_TIMEOUT=10 \
  pg_dump --dbname="$PGDATABASE" --format=custom > "$tmp"
test -s "$tmp"
pg_restore --list "$tmp" >/dev/null
chmod 600 "$tmp"
mv -- "$tmp" "$target"
state="${HOME:?}/.oaos-install/company-state.json"
if [[ -f $state ]]; then
  cp -- "$state" "$OAOS_COMPANY_BACKUP_DIR/company-state-$stamp.json"
  chmod 600 "$OAOS_COMPANY_BACKUP_DIR/company-state-$stamp.json"
fi
env_file="$HOME/.config/oaos-company/.env"
if [[ -f $env_file ]]; then
  cp -- "$env_file" "$OAOS_COMPANY_KEY_BACKUP_DIR/company-env-$stamp"
  chmod 600 "$OAOS_COMPANY_KEY_BACKUP_DIR/company-env-$stamp"
fi

# Rotation: decide the kept stamps from the dumps, then prune every artifact
# whose stamp is not kept. Only names this job creates are considered.
python3 - "$OAOS_COMPANY_BACKUP_DIR" "$OAOS_COMPANY_KEY_BACKUP_DIR" <<'PY'
import datetime, os, re, sys
backup_dir, key_dir = sys.argv[1:3]
pattern = re.compile(r'^company-(\d{8}T\d{6}Z)\.dump$')
stamps = sorted((m.group(1) for m in map(pattern.match, os.listdir(backup_dir)) if m), reverse=True)
keep = set(stamps[:7])
weeks = []
for stamp in stamps[7:]:
    when = datetime.datetime.strptime(stamp, '%Y%m%dT%H%M%SZ')
    week = when.isocalendar()[:2]
    if week not in weeks and len(weeks) < 4 and not any(
            datetime.datetime.strptime(s, '%Y%m%dT%H%M%SZ').isocalendar()[:2] == week for s in keep):
        weeks.append(week)
        keep.add(stamp)
owned = [(backup_dir, re.compile(r'^company-(\d{8}T\d{6}Z)\.dump$')),
         (backup_dir, re.compile(r'^company-state-(\d{8}T\d{6}Z)\.json$')),
         (key_dir, re.compile(r'^company-env-(\d{8}T\d{6}Z)$'))]
for directory, rule in owned:
    for name in os.listdir(directory):
        match = rule.match(name)
        if match and match.group(1) not in keep:
            os.unlink(os.path.join(directory, name))
for directory in (backup_dir, key_dir):
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            os.chmod(path, 0o600)
PY
printf '%s\n' "$stamp" > "$OAOS_COMPANY_BACKUP_DIR/last-success"
chmod 600 "$OAOS_COMPANY_BACKUP_DIR/last-success"
