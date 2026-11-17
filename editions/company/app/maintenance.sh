#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Installed by C17 as the oaos-company-maintenance.service ExecStart (daily).
# company.maintenance_purge() removes audit rows older than 180 days and the
# personal content of members who departed at least 30 days ago, and writes
# its own audit row per organization. It is a SECURITY DEFINER function owned
# by the NOLOGIN oaos_company_maintenance role (migration 023); this job logs in
# as oaos_company_maint, which may only EXECUTE it (no table privilege), over
# loopback TCP with the Company account's 0600 libpq password file. No root or
# postgres account is involved, so the unit runs with NoNewPrivileges.
set -Eeuo pipefail
umask 077
[[ ${PGDATABASE:-} =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || exit 1
port=${PGPORT:-5432}
passfile="${HOME:?}/.config/oaos-company/db-jobs.pgpass"
[[ $port =~ ^[0-9]{1,5}$ && -f $passfile && ! -L $passfile ]] || exit 1
[[ $(stat -c %a "$passfile") == 600 && $(stat -c %u "$passfile") == "$(id -u)" ]] || exit 1
env -u PGOPTIONS -u PGSERVICE -u PGSERVICEFILE -u PGPASSWORD \
  PGHOST=127.0.0.1 PGHOSTADDR=127.0.0.1 PGPORT="$port" PGUSER=oaos_company_maint \
  PGPASSFILE="$passfile" PGCONNECT_TIMEOUT=10 \
  psql --dbname="$PGDATABASE" -X -q -A -t -v ON_ERROR_STOP=1 \
  -c 'SELECT count(*) FROM company.maintenance_purge()' >/dev/null
