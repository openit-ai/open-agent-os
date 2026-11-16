#!/usr/bin/env bash
# Installed by C17 as the oaos-company-maintenance.service ExecStart (daily).
# Retention runs only through the local postgres maintenance account: the
# runtime role has no DELETE on audit rows. company.maintenance_purge() removes
# audit rows older than 180 days and the personal content of members who
# departed at least 30 days ago, and writes its own audit row per organization.
set -Eeuo pipefail
umask 077
[[ ${PGDATABASE:-} =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || exit 1
socket_dir=${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}
port=${PGPORT:-5432}
[[ $socket_dir =~ ^/[A-Za-z0-9_./-]+$ && -d $socket_dir && $port =~ ^[0-9]{1,5}$ ]] || exit 1
maintenance() { if ((EUID == 0)); then "$@"; else sudo -n "$@"; fi; }
maintenance runuser -u postgres -- env -u PGOPTIONS -u PGHOSTADDR -u PGHOST -u PGUSER \
  -u PGSERVICE -u PGPASSFILE -u PGPASSWORD \
  psql --host="$socket_dir" --port="$port" --dbname="$PGDATABASE" -X -q -A -t \
  -v ON_ERROR_STOP=1 -c 'SELECT count(*) FROM company.maintenance_purge()' >/dev/null
