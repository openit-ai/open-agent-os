#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Sourced by company-verify.sh. Never prints EnvironmentFile values.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/lib/company.sh
. "$repo_root/editions/company/lib/company.sh"

company_c02_check() {
  local label=$1
  shift
  if "$@"; then
    printf '%-27s PASS\n' "$label"
    pass=$((pass + 1))
  else
    printf '%-27s FAIL\n' "$label"
    fail=$((fail + 1))
  fi
}
company_c02_load_settings() {
  local file key value seen='' count=0
  file="$HOME/.config/oaos-company/deployment.conf"
  [[ -f $file && $(stat -c %a "$file") == 600 && $(stat -c %U "$file") == "$(id -un)" ]] || return 1
  while IFS='=' read -r key value; do
    case $key in
      OAOS_COMPANY_USER|OAOS_COMPANY_E_ID|PGDATABASE|PGHOST|PGUSER|OAOS_COMPANY_HTTPS_ORIGIN|OAOS_COMPANY_NGINX_VHOST|OAOS_COMPANY_BACKUP_DIR|OAOS_COMPANY_KEY_BACKUP_DIR|OAOS_COMPANY_ROLLBACK_DIR) ;;
      *) return 1 ;;
    esac
    [[ " $seen " != *" $key "* ]] || return 1
    seen+=" $key"
    ((count+=1))
    declare -gx "$key=$value"
  done < "$file"
  [[ $count == 10 && -n ${OAOS_COMPANY_USER:-} && -n ${OAOS_COMPANY_E_ID:-} && -n ${PGDATABASE:-} && -n ${OAOS_COMPANY_NGINX_VHOST:-} ]] || return 1
  # C02-based phases apply schema changes as this NOLOGIN owner role through the
  # local postgres maintenance path; PGUSER stays the non-owning runtime role.
  export OAOS_COMPANY_DB_OWNER=${OAOS_COMPANY_DB_OWNER:-oaos_company_owner}
  [[ $OAOS_COMPANY_DB_OWNER =~ ^[a-z_][a-z0-9_]{0,62}$ ]]
}
# Runtime role boundary (migration 013): the libpq role used by the installer,
# console and service must not own any Company object, must not be able to
# create objects in the schema, and must have only SELECT/INSERT on audit rows.
# Definer boundary (O6/O7, migrations 022+): no SECURITY DEFINER function in the
# schema (console functions once C10+ applied 022) belongs to a superuser, a
# login role, a role with CREATE on the schema or the database (the owner
# role), or a member of the owner role.
company_db_runtime_boundary() {
  local result
  result=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c "
    SELECT NOT EXISTS (
             SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'company' AND pg_has_role(current_user, c.relowner, 'MEMBER'))
       AND NOT EXISTS (
             SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname = 'company' AND pg_has_role(current_user, p.proowner, 'MEMBER'))
       AND NOT pg_has_role(current_user,
             (SELECT nspowner FROM pg_namespace WHERE nspname = 'company'), 'MEMBER')
       AND NOT has_schema_privilege('company', 'CREATE')
       AND has_table_privilege('company.audit_events', 'SELECT')
       AND has_table_privilege('company.audit_events', 'INSERT')
       AND NOT has_table_privilege('company.audit_events', 'UPDATE')
       AND NOT has_table_privilege('company.audit_events', 'DELETE')
       AND NOT has_table_privilege('company.audit_events', 'TRUNCATE')
       AND NOT has_table_privilege('company.schema_migrations', 'INSERT')
       AND NOT EXISTS (
             SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'company' AND c.relkind IN ('r', 'p')
                AND has_table_privilege(c.oid, 'TRUNCATE'))
       AND NOT EXISTS (
             SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
               JOIN pg_roles r ON r.oid = p.proowner
              WHERE n.nspname = 'company' AND p.prosecdef
                AND (p.proname NOT LIKE 'console\\_%' OR EXISTS (
                       SELECT 1 FROM company.schema_migrations WHERE version = 22))
                AND (r.rolsuper OR r.rolcanlogin
                     OR has_schema_privilege(p.proowner, n.oid, 'CREATE')
                     OR has_database_privilege(p.proowner, current_database(), 'CREATE')
                     OR pg_has_role(p.proowner, n.nspowner, 'MEMBER')))" 2>/dev/null) || return 1
  [[ $result == t ]]
}
# Job roles (O7, db-jobs.sql + migration 023): the backup login bypasses RLS
# but holds no write privilege and no other role; the NOLOGIN maintenance role
# has no CREATE anywhere; the maintenance login holds no table privilege and
# no role, and only it (not the runtime) may run the retention purge.
company_db_job_roles_ok() {
  local result
  result=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c "
    SELECT COALESCE((SELECT rolcanlogin AND rolbypassrls AND NOT rolsuper AND NOT rolcreatedb
                        AND NOT rolcreaterole AND NOT rolreplication
                       FROM pg_roles WHERE rolname = 'oaos_company_backup'), false)
       AND COALESCE((SELECT NOT rolcanlogin AND rolbypassrls AND NOT rolsuper AND NOT rolcreatedb
                        AND NOT rolcreaterole
                       FROM pg_roles WHERE rolname = 'oaos_company_maintenance'), false)
       AND COALESCE((SELECT rolcanlogin AND NOT rolbypassrls AND NOT rolsuper AND NOT rolcreatedb
                        AND NOT rolcreaterole
                       FROM pg_roles WHERE rolname = 'oaos_company_maint'), false)
       AND has_table_privilege('oaos_company_backup', 'company.audit_events', 'SELECT')
       AND NOT EXISTS (
             SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'company' AND c.relkind IN ('r', 'p')
                AND (has_table_privilege('oaos_company_backup', c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')
                     OR has_table_privilege('oaos_company_maint', c.oid,
                          'SELECT, INSERT, UPDATE, DELETE, TRUNCATE')))
       AND NOT EXISTS (
             SELECT 1 FROM pg_auth_members m
              WHERE m.member IN (SELECT oid FROM pg_roles WHERE rolname IN
                      ('oaos_company_backup', 'oaos_company_maintenance', 'oaos_company_maint'))
                AND m.roleid::regrole::text <> 'pg_read_all_data')
       AND NOT has_schema_privilege('oaos_company_maintenance', 'company', 'CREATE')
       AND NOT has_database_privilege('oaos_company_maintenance', current_database(), 'CREATE')
       AND NOT has_schema_privilege('oaos_company_backup', 'company', 'CREATE')
       AND NOT has_schema_privilege('oaos_company_maint', 'company', 'CREATE')
       AND has_function_privilege('oaos_company_maint',
             'company.maintenance_purge(integer, integer)', 'EXECUTE')
       AND NOT has_function_privilege('oaos_company_runtime',
             'company.maintenance_purge(integer, integer)', 'EXECUTE')
       AND (SELECT proowner::regrole::text = 'oaos_company_maintenance' AND prosecdef
              FROM pg_proc WHERE oid = 'company.maintenance_purge(integer, integer)'::regprocedure)" \
    2>/dev/null) || return 1
  [[ $result == t ]]
}
# Backup unit (C02) and maintenance unit (C17): hardened, no escalation in the
# job script, and the job password file is the Company account's 0600 file.
company_job_passfile_ok() {
  local file
  file=$(company_db_jobs_passfile)
  [[ -f $file && ! -L $file && $(stat -c %a "$file") == 600 && $(stat -c %U "$file") == "$(id -un)" ]]
}
company_c02_backup_hardened() {
  company_job_unit_hardened "$HOME/.config/systemd/user/oaos-company-backup.service" \
    "$HOME/.local/lib/oaos-company/backup.sh" && company_job_passfile_ok
}
company_c02_unit_ready() {
  [[ $(systemctl --user is-enabled "$1" 2>/dev/null || true) == enabled && $(systemctl --user is-active "$1" 2>/dev/null || true) == active ]]
}
# Runs the real job through systemd and checks what it produced: a fresh
# last-success stamp, a readable custom-format dump, and 0700/0600 modes.
company_c02_backup_job() {
  local dir=${OAOS_COMPANY_BACKUP_DIR:-/nonexistent} key_dir=${OAOS_COMPANY_KEY_BACKUP_DIR:-/nonexistent}
  local started stamp latest
  started=$(date -u +%Y%m%dT%H%M%SZ)
  systemctl --user start oaos-company-backup.service >/dev/null 2>&1 || return 1
  [[ $(systemctl --user show oaos-company-backup.service -p Result --value 2>/dev/null) == success ]] || return 1
  [[ -s $dir/last-success ]] || return 1
  stamp=$(< "$dir/last-success")
  [[ $stamp =~ ^[0-9]{8}T[0-9]{6}Z$ && ! $stamp < $started ]] || return 1
  latest="$dir/company-$stamp.dump"
  [[ -s $latest ]] && pg_restore --list "$latest" >/dev/null 2>&1 || return 1
  [[ $(stat -c %a "$dir") == 700 && $(stat -c %a "$key_dir") == 700 ]] || return 1
  ! find "$dir" "$key_dir" -maxdepth 1 -type f ! -perm 0600 | grep . >/dev/null
}
company_c02_linger() { [[ $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null) == yes ]]; }
company_c02_loopback() {
  local sockets
  sockets=$(ss -ltn 2>/dev/null) || return 1
  grep -Eq '(^|[[:space:]])127\.0\.0\.1:8765[[:space:]]' <<< "$sockets" || return 1
  ! grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\*|\[::\]|::):8765[[:space:]]' <<< "$sockets"
}
company_c02_private_ports() {
  local sockets
  sockets=$(ss -ltn 2>/dev/null) || return 1
  ! awk '$1 == "LISTEN" && $4 ~ /:(5432|6379|8765)$/ && $4 !~ /^(127\.0\.0\.1|\[::1\]|::1):/ {bad=1} END {exit !bad}' <<< "$sockets"
}
company_c02_https() {
  # The origin host may be served by a different machine (shared domain), so a
  # bare 200 is not evidence. Anchor the request to this instance's loopback and
  # require the Company health payload itself.
  local host body
  [[ ${OAOS_COMPANY_HTTPS_ORIGIN:-} =~ ^https://[A-Za-z0-9.-]+$ ]] || return 1
  host=${OAOS_COMPANY_HTTPS_ORIGIN#https://}
  body=$(curl --silent --show-error --noproxy '*' --max-time 8 --resolve "$host:443:127.0.0.1" \
    "$OAOS_COMPANY_HTTPS_ORIGIN/company/health" 2>/dev/null) || return 1
  [[ ${body//[$'\r\n']/} == ok ]]
}
company_c02_rebooted() {
  local before now
  [[ -r $HOME/.config/oaos-company/initial-boot-id ]] || return 1
  before=$(< "$HOME/.config/oaos-company/initial-boot-id")
  now=$(< /proc/sys/kernel/random/boot_id)
  [[ -n $before && -n $now && $before != "$now" ]]
}
company_c02_idempotent() {
  local before after
  before=$(sha256sum "$OAOS_COMPANY_NGINX_VHOST" "$HOME/.config/oaos-company/.env" "$HOME/.config/oaos-company/deployment.conf" "$HOME/.config/systemd/user/oaos-company.service" "$HOME/.config/systemd/user/oaos-company-backup.service" "$HOME/.config/systemd/user/oaos-company-backup.timer" 2>/dev/null) || return 1
  bash "$repo_root/editions/company/install.sh" --stage c02 >/dev/null 2>&1 || return 1
  after=$(sha256sum "$OAOS_COMPANY_NGINX_VHOST" "$HOME/.config/oaos-company/.env" "$HOME/.config/oaos-company/deployment.conf" "$HOME/.config/systemd/user/oaos-company.service" "$HOME/.config/systemd/user/oaos-company-backup.service" "$HOME/.config/systemd/user/oaos-company-backup.timer" 2>/dev/null) || return 1
  [[ $before == "$after" ]]
}
company_c02_verify() {
  local pass=0 fail=0 cert host
  [[ $(stage_status c02) == applied || $(stage_status c02) == verified ]] || { warn 'C02 has not been applied.'; return 3; }
  company_c02_load_settings || { warn 'C02 deployment settings are missing or invalid.'; return 3; }
  if ! { have_cmd systemctl && have_cmd loginctl && have_cmd ss && have_cmd curl && have_cmd sha256sum && have_cmd pg_restore; }; then warn 'C02 read-back commands unavailable.'; return 3; fi
  printf '%-27s %s\n' CHECK RESULT
  company_c02_check 'Service enabled/active' company_c02_unit_ready oaos-company.service
  company_c02_check 'Backup timer enabled/active' company_c02_unit_ready oaos-company-backup.timer
  company_c02_check 'Backup unit hardening' company_c02_backup_hardened
  company_c02_check 'Backup service run' company_c02_backup_job
  company_c02_check 'Linger enabled' company_c02_linger
  company_c02_check 'Loopback port 8765' company_c02_loopback
  company_c02_check 'DB/Redis/Company private' company_c02_private_ports
  company_c02_check 'HTTPS Company health' company_c02_https
  company_c02_check 'Environment permissions' company_env_mode_ok
  host=${OAOS_COMPANY_HTTPS_ORIGIN#https://}
  cert="/etc/letsencrypt/live/$host/fullchain.pem"
  company_c02_check 'TLS expiry (30 days)' run_root openssl x509 -checkend 2592000 -noout -in "$cert"
  company_c02_check 'TLS renewal timer' company_certbot_timer_ok
  company_c02_check 'Installer rerun stable' company_c02_idempotent
  if ((fail)); then printf 'Summary: PASS=%s FAIL=%s MANUAL=1 SKIP=0\n' "$pass" "$fail"; return 1; fi
  if ! company_c02_rebooted; then
    printf '%-27s MANUAL\n' 'Safe reboot and recovery'
    printf 'Summary: PASS=%s FAIL=0 MANUAL=1 SKIP=0\n' "$pass"
    return 3
  fi
  company_c02_check 'Post-reboot service' company_c02_unit_ready oaos-company.service
  company_c02_check 'Post-reboot HTTPS' company_c02_https
  if ((fail)); then printf 'Summary: PASS=%s FAIL=%s MANUAL=0 SKIP=0\n' "$pass" "$fail"; return 1; fi
  printf 'Summary: PASS=%s FAIL=0 MANUAL=0 SKIP=0\n' "$pass"
  stage_mark c02 verified 'E C02 read-back passed after reboot; installer rerun stable'
}
