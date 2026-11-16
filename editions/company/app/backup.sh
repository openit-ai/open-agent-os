#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Installed by C02 as the oaos-company-backup.service ExecStart. The
# EnvironmentFile supplies only non-secret paths and PGDATABASE.
#
# FORCE RLS makes a runtime-role pg_dump fail ("query would be affected by
# row-level security policy"), so the dump runs through the local postgres
# maintenance account, exactly like the C03-C17 pre-migration dumps; the Company
# account only receives the output stream. No credential is passed.
#
# Retention: 7 newest daily dumps plus the newest dump of each of the 4 most
# recent older ISO weeks. The matching state and EnvironmentFile copies follow
# the dumps. Directories are 0700 and files 0600.
set -Eeuo pipefail
umask 077
[[ ${PGDATABASE:-} =~ ^[A-Za-z_][A-Za-z0-9_]*$ && ${OAOS_COMPANY_BACKUP_DIR:-} && ${OAOS_COMPANY_KEY_BACKUP_DIR:-} ]] || exit 1
socket_dir=${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}
port=${PGPORT:-5432}
[[ $socket_dir =~ ^/[A-Za-z0-9_./-]+$ && -d $socket_dir && $port =~ ^[0-9]{1,5}$ ]] || exit 1
maintenance() { if ((EUID == 0)); then "$@"; else sudo -n "$@"; fi; }

mkdir -p "$OAOS_COMPANY_BACKUP_DIR" "$OAOS_COMPANY_KEY_BACKUP_DIR"
chmod 700 "$OAOS_COMPANY_BACKUP_DIR" "$OAOS_COMPANY_KEY_BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$OAOS_COMPANY_BACKUP_DIR/company-$stamp.dump"
tmp=$(mktemp "$target.tmp.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
maintenance runuser -u postgres -- env -u PGOPTIONS -u PGHOSTADDR -u PGHOST -u PGUSER \
  -u PGSERVICE -u PGPASSFILE -u PGPASSWORD \
  pg_dump --host="$socket_dir" --port="$port" --dbname="$PGDATABASE" --format=custom > "$tmp"
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
