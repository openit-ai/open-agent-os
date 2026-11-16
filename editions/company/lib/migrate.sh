#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
# Company runs log to ~/.oaos/logs/company-<date>.log, not the shared install log.
export OAOS_LOG_NAME=${OAOS_LOG_NAME:-company}
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"

usage() {
  printf 'Usage: bash editions/company/lib/migrate.sh [--check] [--through VERSION]\nUses libpq PG* environment or PGSERVICE; set PGDATABASE explicitly.\n'
}
check_only=0
through=0
while (($#)); do
  case $1 in
    --check) check_only=1; shift ;;
    --through)
      (($# >= 2)) && [[ $2 =~ ^[1-9][0-9]*$ ]] || { usage >&2; exit 1; }
      through=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
done
[[ -n ${PGDATABASE:-} ]] || { warn 'PGDATABASE is required for the Company database.'; exit 3; }
if ! have_cmd psql || ! have_cmd sha256sum; then warn 'psql and sha256sum are required.'; exit 3; fi

# libpq reads credentials from its environment/service file. Never pass or print them.
export PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
db_query() { psql -X -q -A -t -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null; }
if ! db_query 'SELECT 1' >/dev/null; then warn 'Company database connection failed.'; exit 3; fi

# Concurrent installer runs (a retried phase, two operators) must not race
# between reading the history and applying a version. A writing run holds a
# session advisory lock on a dedicated connection until it exits; --check only
# reads and takes no lock.
lock_dir='' lock_pid=''
release_lock() {
  if [[ -n $lock_pid ]]; then
    exec 9>&-
    # A connection still waiting for the lock is dropped; the server then
    # releases whatever it holds.
    kill "$lock_pid" 2>/dev/null || true
    wait "$lock_pid" 2>/dev/null || true
    lock_pid=''
  fi
  if [[ -n $lock_dir ]]; then rm -rf -- "$lock_dir"; lock_dir=''; fi
}
acquire_lock() {
  local timeout=${OAOS_COMPANY_MIGRATE_LOCK_TIMEOUT:-300} waited=0
  [[ $timeout =~ ^[0-9]{1,5}$ ]] || die 'OAOS_COMPANY_MIGRATE_LOCK_TIMEOUT must be seconds.'
  lock_dir=$(mktemp -d) || die 'Cannot create the migration lock directory.'
  trap release_lock EXIT
  mkfifo "$lock_dir/in" || die 'Cannot create the migration lock channel.'
  psql -X -q -A -t -v ON_ERROR_STOP=1 < "$lock_dir/in" > "$lock_dir/out" 2>/dev/null &
  lock_pid=$!
  exec 9>"$lock_dir/in"
  printf '%s\n' "SELECT pg_advisory_lock(hashtext('oaos-company-migrate'));" '\echo locked' >&9
  until grep -x locked "$lock_dir/out" >/dev/null 2>&1; do
    kill -0 "$lock_pid" 2>/dev/null || die 'Company migration lock connection failed.'
    ((waited < timeout * 10)) || die 'Another Company migration run holds the migration lock.'
    sleep 0.1
    waited=$((waited + 1))
  done
}

files=("$repo_root"/editions/company/migrations/[0-9][0-9][0-9]_*.sql)
[[ -f ${files[0]} ]] || die 'Company migration files are missing.'

# Two kinds of migration files:
# - base migrations (001-012) form the strictly monotonic phase chain;
# - amendments (013+) carry "-- oaos-applied-with: N" and are applied by every
#   phase whose --through covers base version N. They harden objects created by
#   base version N without editing that checksummed file, so a phase can apply
#   its fix without also applying later phases' base migrations.
#
# OAOS_COMPANY_DB_OWNER (set by C02-based phases) selects the privileged path:
# schema changes run through the local postgres maintenance account as the
# NOLOGIN owner role, so the runtime role (libpq PGUSER) never owns Company
# objects. Without it only base migrations are applied as the libpq role (C01
# and test fixtures); amendments then stay pending for the next phase.
owner=${OAOS_COMPANY_DB_OWNER:-}
if [[ -n $owner && ! $owner =~ ^[a-z_][a-z0-9_]{0,62}$ ]]; then die 'OAOS_COMPANY_DB_OWNER must be a simple role identifier.'; fi
socket_dir=${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}
privileged_psql() {
  run_root runuser -u postgres -- env -u PGOPTIONS -u PGHOSTADDR -u PGHOST -u PGUSER \
    -u PGSERVICE -u PGPASSFILE -u PGPASSWORD \
    psql --host="$socket_dir" --port="${PGPORT:-5432}" --dbname="$PGDATABASE" \
    -X -q -v ON_ERROR_STOP=1 "$@"
}
roles_ready=0
prepare_roles() {
  local runtime
  ((roles_ready)) && return 0
  [[ $socket_dir =~ ^/[A-Za-z0-9_./-]+$ && -d $socket_dir && ${PGPORT:-5432} =~ ^[0-9]{1,5}$ ]] || die 'Local PostgreSQL maintenance socket or port is invalid.'
  runtime=$(db_query 'SELECT current_user') || die 'Cannot identify the runtime role.'
  [[ $runtime =~ ^[a-z_][a-z0-9_]{0,62}$ && $runtime != "$owner" ]] || die 'Runtime role must be a simple identifier distinct from the owner role.'
  privileged_psql --single-transaction -v owner="$owner" -v runtime="$runtime" \
    -f "$repo_root/editions/company/migrations/db-roles.sql" >/dev/null 2>&1 || die 'Company owner/runtime role separation failed.'
  roles_ready=1
}
apply_file() {
  local file=$1 version=$2 checksum=$3
  if [[ -n $owner ]]; then
    prepare_roles
    privileged_psql --single-transaction -c "SET ROLE \"$owner\"" -f "$file" \
      -c "INSERT INTO company.schema_migrations(version, checksum) VALUES ($version, '$checksum')" \
      -c 'GRANT USAGE ON SCHEMA company TO oaos_company_runtime' \
      -c 'GRANT SELECT ON company.schema_migrations TO oaos_company_runtime' \
      >/dev/null 2>&1
  else
    psql -X -q -v ON_ERROR_STOP=1 --single-transaction -f "$file" \
      -c "INSERT INTO company.schema_migrations(version, checksum) VALUES ($version, '$checksum')" \
      >/dev/null 2>&1
  fi
}
file_checksum() {
  local checksum
  checksum=$(sha256sum "$1") || die 'Cannot checksum a migration.'
  checksum=${checksum%% *}
  [[ $checksum =~ ^[0-9a-f]{64}$ ]] || die 'Invalid migration checksum.'
  printf '%s' "$checksum"
}

base_files=() amend_files=() amend_with=()
first_amendment=0
last_seen_version=0
for file in "${files[@]}"; do
  name=${file##*/}
  version=$((10#${name:0:3}))
  # Versions must increase. A number may be reserved by a parallel phase and be
  # absent here; the applied history still has to be strictly monotonic.
  ((version > last_seen_version)) || die 'Migration file versions must increase.'
  last_seen_version=$version
  with=$(sed -n '1,5s/^-- oaos-applied-with: \([0-9][0-9]*\)$/\1/p' "$file")
  if [[ -n $with ]]; then
    ((with < version)) || die 'An amendment must follow the base migration it hardens.'
    ((first_amendment)) || first_amendment=$version
    amend_files+=("$file"); amend_with+=("$with")
  else
    ((first_amendment == 0)) || die 'Base migrations must precede amendments.'
    base_files+=("$file")
  fi
done
((first_amendment)) || first_amendment=$((last_seen_version + 1))

((check_only)) || acquire_lock

last_file_version=0
expected_versions=()
for file in "${base_files[@]}"; do
  name=${file##*/}
  version=$((10#${name:0:3}))
  if ((through && version > through)); then continue; fi
  last_file_version=$version
  expected_versions+=("$version")
  checksum=$(file_checksum "$file")
  table_exists=$(db_query "SELECT to_regclass('company.schema_migrations') IS NOT NULL") || die 'Cannot inspect migration state.'
  if [[ $table_exists == t ]]; then
    current=$(db_query "SELECT checksum FROM company.schema_migrations WHERE version = $version") || die 'Cannot read migration checksum.'
    if [[ -n $current ]]; then
      [[ $current == "$checksum" ]] || die "Migration checksum mismatch at version $version."
      info "Migration $version checksum matches; skipped."
      continue
    fi
    max_version=$(db_query "SELECT COALESCE(MAX(version), 0) FROM company.schema_migrations WHERE version < $first_amendment") || die 'Cannot read migration version.'
    ((version > max_version)) || die "Migration version $version is not monotonic."
  elif [[ $table_exists != f ]]; then
    die 'Cannot identify migration state.'
  elif ((version != 1)); then
    die 'The first migration must have version 1.'
  fi
  if ((check_only)); then
    info "Migration $version is pending."
    exit 3
  fi
  apply_file "$file" "$version" "$checksum" || die "Migration $version failed; transaction rolled back."
  info "Migration $version applied."
done
if ((through && last_file_version != through)); then die 'Requested migration version is not available.'; fi
history=$(db_query "SELECT COALESCE(MAX(version), 0) || ':' || COUNT(*) FROM company.schema_migrations WHERE version <= $last_file_version") || die 'Cannot inspect migration history.'
[[ $history == "$last_file_version:${#expected_versions[@]}" ]] || die 'Database migration history has an unknown version, gap or missing version.'

for i in "${!amend_files[@]}"; do
  file=${amend_files[$i]}
  name=${file##*/}
  version=$((10#${name:0:3}))
  if ((through && amend_with[i] > through)) || ((amend_with[i] > last_file_version)); then continue; fi
  checksum=$(file_checksum "$file")
  current=$(db_query "SELECT checksum FROM company.schema_migrations WHERE version = $version") || die 'Cannot read migration checksum.'
  if [[ -n $current ]]; then
    [[ $current == "$checksum" ]] || die "Migration checksum mismatch at version $version."
    info "Migration $version checksum matches; skipped."
    continue
  fi
  if [[ -z $owner ]]; then
    # Amendments need the owner/runtime separation of the C02-based phases.
    info "Migration $version (amends version ${amend_with[i]}) is pending for the phase installer."
    continue
  fi
  if ((check_only)); then
    info "Migration $version is pending."
    exit 3
  fi
  apply_file "$file" "$version" "$checksum" || die "Migration $version failed; transaction rolled back."
  info "Migration $version applied."
done
