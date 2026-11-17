#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Shared Company helpers. Sourced (after bootstrap/lib/common.sh) by the phase
# scripts, verify scripts and install.sh. Never prints credentials.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}

# Absolute, normalized path without traversal or shell metacharacters.
company_valid_path() {
  [[ $1 =~ ^/[A-Za-z0-9_./-]+$ && $1 != / && $1 != *'//'* && $1 != *'/../'* && $1 != *'/./'* ]]
}

# Run a command as the local postgres maintenance account. libpq connection
# variables of the Company runtime role are scrubbed so nothing leaks into or
# redirects the maintenance connection; no credential is passed.
company_maint() {
  run_root runuser -u postgres -- env -u PGOPTIONS -u PGHOSTADDR -u PGHOST -u PGUSER \
    -u PGSERVICE -u PGPASSFILE -u PGPASSWORD "$@"
}
company_maint_socket() { printf '%s' "${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}"; }
company_maint_socket_ok() {
  local socket_dir
  socket_dir=$(company_maint_socket)
  company_valid_path "$socket_dir" && [[ -d $socket_dir && ${PGPORT:-5432} =~ ^[0-9]{1,5}$ ]]
}
# Unaligned, tuples-only query through the maintenance account (BYPASSRLS
# reads used to locate the G10 organization before a session context exists).
company_maint_query() {
  company_maint psql --host="$(company_maint_socket)" --port="${PGPORT:-5432}" \
    --dbname="$PGDATABASE" -X -q -A -t -c "$1" 2>/dev/null
}
# Full custom-format dump of E through the maintenance account (FORCE RLS makes
# a runtime-role pg_dump incomplete). The output file is created 0600.
company_maint_dump() {
  local target=$1
  (umask 077; company_maint pg_dump --host="$(company_maint_socket)" --port="${PGPORT:-5432}" \
    --dbname="$PGDATABASE" --format=custom > "$target" 2>/dev/null) || return 1
  [[ -s $target ]] || return 1
  chmod 600 "$target"
}

# Job roles (O7). The daily backup and retention jobs, the C17 recovery drill's
# key check and the vault rotation connect over loopback TCP as a dedicated
# login role with the Company account's 0600 libpq password file, so no job
# needs root or the postgres account. company_db_job <role> <command...> runs a
# command with only that role's connection settings.
company_db_jobs_passfile() { printf '%s' "$HOME/.config/oaos-company/db-jobs.pgpass"; }
company_db_job() {
  local role=$1
  shift
  env -u PGOPTIONS -u PGSERVICE -u PGSERVICEFILE -u PGPASSWORD -u PGSSLMODE \
    PGHOST=127.0.0.1 PGHOSTADDR=127.0.0.1 PGPORT="${PGPORT:-5432}" PGUSER="$role" \
    PGPASSFILE="$(company_db_jobs_passfile)" PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-5}" "$@"
}
company_db_job_login() {
  [[ $(company_db_job "$1" psql -X -q -A -t --dbname="$PGDATABASE" -c 'SELECT 1' 2>/dev/null) == 1 ]]
}
# Creates or re-asserts the job roles (db-jobs.sql) through the maintenance
# account and, when a job login does not work yet, stores generated passwords
# in the password file and sends only their SCRAM verifiers on standard input.
company_db_jobs_ensure() {
  local socket file port=${PGPORT:-5432}
  socket=$(company_maint_socket)
  file=$(company_db_jobs_passfile)
  company_maint_socket_ok || return 1
  company_maint psql --host="$socket" --port="$port" --dbname="$PGDATABASE" -X -q \
    -v ON_ERROR_STOP=1 --single-transaction \
    -f "$repo_root/editions/company/migrations/db-jobs.sql" >/dev/null 2>&1 || return 1
  if [[ -f $file ]] && company_db_job_login oaos_company_backup &&
     company_db_job_login oaos_company_maint; then
    return 0
  fi
  python3 "$repo_root/editions/company/app/db-jobs.py" ensure "$file" "$port" |
    company_maint psql --host="$socket" --port="$port" --dbname="$PGDATABASE" -X -q \
      -v ON_ERROR_STOP=1 --single-transaction >/dev/null 2>&1 || return 1
  company_db_job_login oaos_company_backup && company_db_job_login oaos_company_maint
}

# Hardening lines of the backup and maintenance oneshot units (O7). Both jobs
# run without root, so they keep NoNewPrivileges. [writable paths] become the
# only ReadWritePaths. company_job_unit_hardened checks a unit and its script.
# shellcheck disable=SC2120 # C02 passes the writable backup paths
company_job_unit_hardening() {
  printf '%s\n' NoNewPrivileges=yes PrivateTmp=yes ProtectSystem=strict \
    ProtectHome=read-only RestrictSUIDSGID=yes LockPersonality=yes
  if [[ -n ${1:-} ]]; then printf 'ReadWritePaths=%s\n' "$1"; fi
}
company_job_unit_hardened() {
  local unit=$1 script=$2 line
  [[ -f $unit && -f $script ]] || return 1
  while IFS= read -r line; do
    [[ $line == ReadWritePaths=* ]] && continue
    grep -Fxq "$line" "$unit" || return 1
  done < <(company_job_unit_hardening)
  # A job that still escalates (sudo, runuser, su) cannot run under these units.
  ! grep -v '^[[:space:]]*#' "$script" | grep -Eq '(^|[^A-Za-z0-9_-])(sudo|runuser|su)([^A-Za-z0-9_-]|$)'
}

# Runtime-role statement inside the session context of one organization and
# administrator. Prints the last non-empty output line.
company_sql() {
  local org=$1 admin=$2 statement=$3 output line last=''
  output=$(psql -X -q -A -t -v ON_ERROR_STOP=1 \
    -c "SELECT set_config('company.org_id', '$org', false);" \
    -c "SELECT set_config('company.member_id', '$admin', false);" \
    -c "$statement" 2>/dev/null) || return 1
  while IFS= read -r line; do [[ -n $line ]] && last=$line; done <<< "$output"
  printf '%s' "$last"
}
# Like company_sql for a single value: an empty result is distinguishable from
# the preceding set_config output (always one marked line).
company_sql_value() {
  local org=$1 admin=$2 statement=$3 output last
  output=$(psql -X -q -A -t -v ON_ERROR_STOP=1 \
    -c "SELECT set_config('company.org_id', '$org', false);" \
    -c "SELECT set_config('company.member_id', '$admin', false);" \
    -c "SELECT '__OAOS__' || COALESCE(($statement)::text, '')" 2>/dev/null) || return 1
  last=${output##*$'\n'}
  [[ $last == __OAOS__* ]] || return 1
  printf '%s' "${last#__OAOS__}"
}

# The single G10 organization and its longest-standing active administrator,
# printed as "org admin". Fails for zero or several organizations.
company_admin_context() {
  local rows row org='' admin='' first_org=''
  rows=$(company_maint_query "SELECT m.org_id::text || '|' || m.id::text
          FROM company.members m
          JOIN company.organizations o ON o.id = m.org_id
         WHERE m.role = 'admin' AND m.state = 'active' AND o.state = 'active'
         ORDER BY o.created_at, m.created_at, m.id LIMIT 2") || return 1
  while IFS= read -r row; do
    [[ -n $row ]] || continue
    org=${row%%|*}
    # Keep the first (longest-standing) administrator; never the last row read.
    [[ -n $admin ]] || admin=${row#*|}
    [[ -z $first_org ]] && first_org=$org
    [[ $org != "$first_org" ]] && return 1
  done <<< "$rows"
  [[ $first_org =~ ^[0-9a-f-]{36}$ && $admin =~ ^[0-9a-f-]{36}$ ]] || return 1
  printf '%s %s\n' "$first_org" "$admin"
}

# Company EnvironmentFile 0600 (file) and its directory 0700 (mode).
company_env_file_ok() { [[ $(stat -c %a "$HOME/.config/oaos-company/.env" 2>/dev/null) == 600 ]]; }
company_env_mode_ok() {
  [[ $(stat -c %a "$HOME/.config/oaos-company" 2>/dev/null) == 700 ]] && company_env_file_ok
}

# The existing Project certificate renewal timer (apt or snap certbot).
company_certbot_timer_ok() {
  local unit
  for unit in certbot.timer snap.certbot.renew.timer; do
    if [[ $(systemctl is-enabled "$unit" 2>/dev/null || true) == enabled &&
          $(systemctl is-active "$unit" 2>/dev/null || true) == active ]]; then
      return 0
    fi
  done
  return 1
}

# "C01, C02 and C03" from "c01 c02 c03".
company_phase_list() {
  local -a names=()
  local item
  for item in "$@"; do names+=("$(printf '%s' "$item" | tr '[:lower:]' '[:upper:]')"); done
  local count=${#names[@]} i out=''
  for ((i = 0; i < count; i++)); do
    if ((i == 0)); then out=${names[i]}
    elif ((i == count - 1)); then out+=" and ${names[i]}"
    else out+=", ${names[i]}"; fi
  done
  printf '%s' "$out"
}

# company_readback <sql file> [psql option...]
# Runs a read-back file as the runtime role. A read-back marks every section
# that really passed with RAISE NOTICE / \echo "oaos-check: <name>", so the
# verify scripts print a PASS line only for a check whose query answered.
# COMPANY_READBACK holds the emitted names; the output itself is discarded.
COMPANY_READBACK=''
company_readback() {
  local file=$1 output rc=0
  shift
  output=$(psql -X -q -v ON_ERROR_STOP=1 "$@" -f "$file" 2>&1) || rc=$?
  COMPANY_READBACK=$(printf '%s\n' "$output" | sed -n 's/^.*oaos-check: \([a-z0-9_]*\).*$/\1/p')
  return "$rc"
}
company_readback_passed() { [[ $'\n'$COMPANY_READBACK$'\n' == *$'\n'"$1"$'\n'* ]]; }
# company_readback_line <number> <check label> <marker> <evidence>
# Prints one result row and counts it in the caller's pass/fail variables.
company_readback_line() {
  if company_readback_passed "$3"; then
    printf '%-3s %-24s %-7s %s\n' "$1" "$2" PASS "$4"; pass=$((pass + 1))
  else
    printf '%-3s %-24s %-7s %s\n' "$1" "$2" FAIL "$4"; fail=$((fail + 1))
  fi
}
# company_readback_check <width> <label> <marker...>
# "<label> PASS" only when every marker was emitted; counts pass/fail.
company_readback_check() {
  local width=$1 label=$2 marker ok=1
  shift 2
  for marker in "$@"; do company_readback_passed "$marker" || ok=0; done
  if ((ok)); then
    printf "%-${width}s PASS\n" "$label"; pass=$((pass + 1))
  else
    printf "%-${width}s FAIL\n" "$label"; fail=$((fail + 1))
  fi
}
