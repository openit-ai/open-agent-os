#!/usr/bin/env bash
# Sourced by install.sh. C06 uses the C02 E host and the C04/C05 identity boundary.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/c02.sh
. "$repo_root/editions/company/c02.sh"
# shellcheck source=editions/company/c02-verify.sh
. "$repo_root/editions/company/c02-verify.sh"

company_c06_apply() {
  local ready check_code=0 role_safe snapshot socket_dir
  [[ $(stage_status c01) == verified && $(stage_status c02) == verified && \
     $(stage_status c03) == verified && $(stage_status c04) == verified && \
     $(stage_status c05) == verified ]] || {
    warn 'C06 BLOCKED: C01, C02, C03, C04 and C05 must be verified on E.'; return 3;
  }
  company_c02_load_settings || { warn 'C06 BLOCKED: C02 E deployment settings are invalid.'; return 3; }
  company_c02_preflight || return 3
  export PGHOSTADDR=127.0.0.1
  if ! have_cmd psql || ! have_cmd pg_dump || ! have_cmd sha256sum; then
    warn 'C06 BLOCKED: PostgreSQL tools are missing.'; return 3
  fi
  export PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  ready=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' 2>/dev/null) || {
    warn 'C06 BLOCKED: E database connection failed.'; return 3;
  }
  [[ $ready == 1 ]] || { warn 'C06 BLOCKED: E database did not answer.'; return 3; }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'C06 BLOCKED: database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'C06 BLOCKED: database role bypasses RLS.'; return 3; }
  # Migrations are global and consecutive, so reaching version 6 also applies the
  # C16/G13 schema from version 5 when it is still pending. It carries no keys or
  # configuration and is harmless before the C16 stage runs.
  bash "$repo_root/editions/company/migrate.sh" --check --through 6 >/dev/null 2>&1 || check_code=$?
  case $check_code in
    0) info 'C06 migration checksum confirmed; no database change needed.'; return 0 ;;
    3) ;;
    *) warn 'C06 migration history or checksum is invalid.'; return 1 ;;
  esac
  snapshot=$(mktemp -d "$OAOS_COMPANY_ROLLBACK_DIR/c06-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX") || return 1
  chmod 700 "$snapshot"
  socket_dir=${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}
  if ! company_c02_valid_path "$socket_dir" || [[ ! -d $socket_dir ]] ||
     [[ ! ${PGPORT:-5432} =~ ^[0-9]{1,5}$ ]]; then
    warn 'C06 BLOCKED: local PostgreSQL backup socket or port is invalid.'; return 3
  fi
  # FORCE RLS makes an app-role pg_dump incomplete or impossible. Use the
  # existing local postgres maintenance account for the full pre-upgrade dump.
  # The Company account creates the restricted output file; no credential is passed.
  if ! (umask 077; run_root runuser -u postgres -- env -u PGOPTIONS -u PGHOSTADDR \
      -u PGHOST -u PGUSER -u PGSERVICE -u PGPASSFILE -u PGPASSWORD \
      pg_dump --host="$socket_dir" --port="${PGPORT:-5432}" \
      --dbname="$PGDATABASE" --format=custom > "$snapshot/company.dump" 2>/dev/null) ||
      [[ ! -s $snapshot/company.dump ]]; then
    warn 'C06 BLOCKED: E database backup failed or is empty.'; return 3
  fi
  chmod 600 "$snapshot/company.dump"
  bash "$repo_root/editions/company/migrate.sh" --through 6 || return 1
  info 'C06 identity isolation migration applied; run E identity read-back.'
}
