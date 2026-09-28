#!/usr/bin/env bash
# Sourced by install.sh. C03 uses the C02 E host and its dedicated account.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/c02.sh
. "$repo_root/editions/company/c02.sh"
# shellcheck source=editions/company/c02-verify.sh
. "$repo_root/editions/company/c02-verify.sh"

company_c03_apply() {
  local ready check_code=0 role_safe snapshot socket_dir
  [[ $(stage_status c01) == verified && $(stage_status c02) == verified ]] || {
    warn 'C03 BLOCKED: C01 and C02 must be verified on E.'; return 3;
  }
  company_c02_load_settings || { warn 'C03 BLOCKED: C02 E deployment settings are invalid.'; return 3; }
  company_c02_preflight || return 3
  export PGHOSTADDR=127.0.0.1
  if ! have_cmd psql || ! have_cmd pg_dump || ! have_cmd sha256sum; then
    warn 'C03 BLOCKED: PostgreSQL tools are missing.'; return 3
  fi
  export PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  ready=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' 2>/dev/null) || {
    warn 'C03 BLOCKED: E database connection failed.'; return 3;
  }
  [[ $ready == 1 ]] || { warn 'C03 BLOCKED: E database did not answer.'; return 3; }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'C03 BLOCKED: database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'C03 BLOCKED: database role bypasses RLS.'; return 3; }
  bash "$repo_root/editions/company/migrate.sh" --check --through 2 >/dev/null 2>&1 || check_code=$?
  case $check_code in
    0) info 'C03 migration checksum confirmed; no database change needed.'; return 0 ;;
    3) ;;
    *) warn 'C03 migration history or checksum is invalid.'; return 1 ;;
  esac
  snapshot=$(mktemp -d "$OAOS_COMPANY_ROLLBACK_DIR/c03-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX") || return 1
  chmod 700 "$snapshot"
  socket_dir=${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}
  if ! company_c02_valid_path "$socket_dir" || [[ ! -d $socket_dir ]] ||
     [[ ! ${PGPORT:-5432} =~ ^[0-9]{1,5}$ ]]; then
    warn 'C03 BLOCKED: local PostgreSQL backup socket or port is invalid.'; return 3
  fi
  # FORCE RLS makes an app-role pg_dump incomplete or impossible. Use the
  # existing local postgres maintenance account for the full pre-upgrade dump.
  # The Company account creates the restricted output file; no credential is passed.
  if ! (umask 077; run_root runuser -u postgres -- env -u PGOPTIONS -u PGHOSTADDR \
      -u PGHOST -u PGUSER -u PGSERVICE -u PGPASSFILE -u PGPASSWORD \
      pg_dump --host="$socket_dir" --port="${PGPORT:-5432}" \
      --dbname="$PGDATABASE" --format=custom > "$snapshot/company.dump" 2>/dev/null) ||
      [[ ! -s $snapshot/company.dump ]]; then
    warn 'C03 BLOCKED: E database backup failed or is empty.'; return 3
  fi
  chmod 600 "$snapshot/company.dump"
  bash "$repo_root/editions/company/migrate.sh" --through 2 || return 1
  info 'C03 migration applied; run E mapping read-back.'
}
