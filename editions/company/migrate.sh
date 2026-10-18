#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"

usage() {
  printf 'Usage: bash editions/company/migrate.sh [--check] [--through VERSION]\nUses libpq PG* environment or PGSERVICE; set PGDATABASE explicitly.\n'
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

files=("$repo_root"/editions/company/migrations/[0-9][0-9][0-9]_*.sql)
[[ -f ${files[0]} ]] || die 'Company migration files are missing.'
last_file_version=0
last_seen_version=0
for file in "${files[@]}"; do
  name=${file##*/}
  version=$((10#${name:0:3}))
  ((version == last_seen_version + 1)) || die 'Migration file versions must be consecutive.'
  last_seen_version=$version
  if ((through && version > through)); then continue; fi
  last_file_version=$version
  checksum=$(sha256sum "$file") || die 'Cannot checksum a migration.'
  checksum=${checksum%% *}
  [[ $checksum =~ ^[0-9a-f]{64}$ ]] || die 'Invalid migration checksum.'
  table_exists=$(db_query "SELECT to_regclass('company.schema_migrations') IS NOT NULL") || die 'Cannot inspect migration state.'
  if [[ $table_exists == t ]]; then
    current=$(db_query "SELECT checksum FROM company.schema_migrations WHERE version = $version") || die 'Cannot read migration checksum.'
    if [[ -n $current ]]; then
      [[ $current == "$checksum" ]] || die "Migration checksum mismatch at version $version."
      info "Migration $version checksum matches; skipped."
      continue
    fi
    max_version=$(db_query 'SELECT COALESCE(MAX(version), 0) FROM company.schema_migrations') || die 'Cannot read migration version.'
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
  if ! psql -X -q -v ON_ERROR_STOP=1 --single-transaction -f "$file" \
      -c "INSERT INTO company.schema_migrations(version, checksum) VALUES ($version, '$checksum')" \
      >/dev/null 2>&1; then
    die "Migration $version failed; transaction rolled back."
  fi
  info "Migration $version applied."
done
if ((through && last_file_version != through)); then die 'Requested migration version is not available.'; fi
history=$(db_query "SELECT COALESCE(MAX(version), 0) || ':' || COUNT(*) FROM company.schema_migrations WHERE version <= $last_file_version") || die 'Cannot inspect migration history.'
[[ $history == "$last_file_version:$last_file_version" ]] || die 'Database migration history has an unknown version or gap.'
