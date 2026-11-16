#!/usr/bin/env bash
# Shared Company helpers. Sourced (after bootstrap/lib/common.sh) by the phase
# scripts, verify scripts and install.sh. Never prints credentials.

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
