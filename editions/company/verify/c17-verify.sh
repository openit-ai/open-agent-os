#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Sourced by company-verify.sh. Uses only E loopback PostgreSQL and local units.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/stages/c17.sh
. "$repo_root/editions/company/stages/c17.sh"

company_c17_verify() {
  local role_safe result=0 org='' admin='' handover='' handover_state=''
  local service_state='' reboot_verified='' boot_id='' previous_boot_id=''
  local latest='' backup_id='' location='' checksum='' size='' recovery_id=''
  local selected_count='0' missing='0' pass=0 skip=0
  local current_boot='' backup_result=0

  [[ $(stage_status c02) == verified && $(stage_status c10) == verified && \
     $(stage_status c15) == verified && ( $(stage_status c16) == verified || $(stage_status c16) == SKIP ) ]] || {
    warn 'C17 read-back needs C02, C10, C15 and C16 verified on E.'; return 3;
  }
  [[ $(stage_status c17) == applied || $(stage_status c17) == verified ]] || {
    warn 'C17 has not been applied.'; return 3;
  }
  company_c02_load_settings || { warn 'C02 E deployment settings are invalid.'; return 3; }
  [[ $PGHOST == 127.0.0.1 || $PGHOST == localhost ]] || {
    warn 'C17 read-back requires loopback PostgreSQL.'; return 3;
  }
  export PGHOSTADDR=127.0.0.1 PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  have_cmd psql || { warn 'psql is required for C17 read-back.'; return 3; }
  psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1 || {
    warn 'E database connection failed.'; return 3;
  }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'C17 database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'C17 read-back needs a non-BYPASSRLS role.'; return 3; }

  bash "$repo_root/editions/company/lib/migrate.sh" --check --through 12 >/dev/null 2>&1 || result=$?
  if ((result)); then
    if ((result == 3)); then warn 'C17 migration is pending.'; return 3; fi
    warn 'C17 migration checksum or version read-back failed.'; return 1
  fi
  read -r org admin < <(company_admin_context) || true
  [[ -n $org && -n $admin ]] || {
    warn 'C17 read-back needs a single organization with an active administrator.'; return 3;
  }

  handover=$(company_sql_value "$org" "$admin" \
    "SELECT state || '|' || service_state || '|' || reboot_verified || '|'
            || COALESCE(boot_id, '') || '|' || COALESCE(previous_boot_id, '')
       FROM company.handover_state WHERE org_id = '$org'::uuid") || handover=''
  if [[ -z $handover ]]; then
    printf '%-30s MANUAL\n' 'Persisted handover evidence'
    printf 'Summary: PASS=0 FAIL=0 MANUAL=1 SKIP=0\n'
    return 3
  fi
  IFS='|' read -r handover_state service_state reboot_verified boot_id previous_boot_id <<< "$handover"
  if [[ $handover_state != verified || $service_state != active ||
        $reboot_verified != true || -z $boot_id || -z $previous_boot_id ||
        $boot_id == "$previous_boot_id" ]]; then
    printf '%-30s MANUAL\n' 'Persisted handover evidence'
    printf 'Summary: PASS=0 FAIL=0 MANUAL=1 SKIP=0\n'
    return 3
  fi
  current_boot=$(tr -d '\r\n' < /proc/sys/kernel/random/boot_id 2>/dev/null || true)
  if [[ -z $current_boot ]]; then
    printf '%-30s FAIL\n' 'Boot ID read-back'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi

  latest=$(company_c17_latest_backup "$org" "$admin") || latest=''
  if [[ -z $latest ]]; then
    printf '%-30s FAIL\n' 'Verified backup record'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  IFS='|' read -r backup_id location checksum size <<< "$latest"
  if ! company_c17_file_ok "$location" "$checksum" "$size"; then
    printf '%-30s FAIL\n' 'Backup file and checksum'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  have_cmd pg_restore || { warn 'pg_restore is required for C17 read-back.'; return 3; }
  pg_restore --list "$location" >/dev/null 2>&1 || backup_result=1
  if ((backup_result)); then
    printf '%-30s FAIL\n' 'Backup archive readability'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi

  recovery_id=$(company_c17_latest_recovery "$org" "$admin" "$backup_id") || recovery_id=''
  if [[ -z $recovery_id ]]; then
    printf '%-30s FAIL\n' 'Passing recovery drill'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi

  selected_count=$(company_sql_value "$org" "$admin" \
    "SELECT count(*) FROM company.integration_settings
      WHERE org_id = '$org'::uuid AND state = 'selected'") || selected_count='0'
  missing=$(company_sql_value "$org" "$admin" \
    "SELECT count(*) FROM company.integration_settings s
      WHERE s.org_id = '$org'::uuid AND s.state = 'selected'
        AND COALESCE((SELECT h.external_roundtrip->>s.provider
                        FROM company.handover_state h WHERE h.org_id = s.org_id), '') <> 'confirmed'") || missing='0'
  if [[ ! $missing =~ ^0$ ]]; then
    printf '%-30s MANUAL\n' 'External round-trip evidence'
    printf 'Summary: PASS=0 FAIL=0 MANUAL=1 SKIP=0\n'
    return 3
  fi

  if [[ $(systemctl --user is-enabled oaos-company.service 2>/dev/null || true) != enabled ||
        $(systemctl --user is-active oaos-company.service 2>/dev/null || true) != active ||
        $(systemctl --user is-enabled oaos-company-backup.timer 2>/dev/null || true) != enabled ||
        $(systemctl --user is-active oaos-company-backup.timer 2>/dev/null || true) != active ]]; then
    printf '%-30s FAIL\n' 'Service and backup timer'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi

  # RPO: the daily backup job succeeded within the last 24 hours.
  local last_success age
  last_success=$(cat "${OAOS_COMPANY_BACKUP_DIR:-/nonexistent}/last-success" 2>/dev/null || true)
  age=$(python3 - "$last_success" <<'PY' 2>/dev/null || true
import datetime, sys
stamp = datetime.datetime.strptime(sys.argv[1].strip(), '%Y%m%dT%H%M%SZ').replace(tzinfo=datetime.timezone.utc)
print(int((datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds()))
PY
)
  if [[ ! $age =~ ^[0-9]+$ ]] || ((age > 86400)); then
    printf '%-30s FAIL\n' 'Daily backup within 24h'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  # Retention: the maintenance timer is on and purged within the last 48 hours.
  local retention
  retention=$(company_sql_value "$org" "$admin" \
    "SELECT count(*) FROM company.audit_events
      WHERE org_id = '$org'::uuid AND event_type = 'maintenance'
        AND action = 'retention_purged' AND at > now() - interval '48 hours'") || retention=0
  if [[ $(systemctl --user is-enabled oaos-company-maintenance.timer 2>/dev/null || true) != enabled ||
        $(systemctl --user is-active oaos-company-maintenance.timer 2>/dev/null || true) != active ||
        ! $retention =~ ^[1-9][0-9]*$ ]]; then
    printf '%-30s FAIL\n' 'Retention timer and last purge'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi

  # O6/O7: definer functions and the job roles keep least privilege, and the
  # backup and retention units run hardened without root.
  if ! company_db_runtime_boundary || ! company_db_job_roles_ok; then
    printf '%-30s FAIL\n' 'Job and definer roles'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi
  if ! company_c02_backup_hardened ||
     ! company_job_unit_hardened "$HOME/.config/systemd/user/oaos-company-maintenance.service" \
       "$HOME/.local/lib/oaos-company/maintenance.sh"; then
    printf '%-30s FAIL\n' 'Job unit hardening'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi

  local readback_error readback_code=0
  readback_error=$(mktemp)
  if ! psql -X -q -v ON_ERROR_STOP=1 -v org="$org" -v admin="$admin" \
      -f "$repo_root/editions/company/readback/c17-readback.sql" >"$readback_error" 2>&1; then
    readback_code=1
  fi
  if [[ ${OAOS_C17_VERIFY_DEBUG:-0} == 1 && $readback_code == 1 ]]; then
    cat "$readback_error" >&2 || true
  fi
  rm -f -- "$readback_error"
  if ((readback_code)); then
    printf '%-30s FAIL\n' 'Handover and revocation queries'
    printf 'Summary: PASS=0 FAIL=1 MANUAL=0 SKIP=0\n'
    return 1
  fi

  printf '%-30s PASS\n' 'Migration SHA-256/version 12'; pass=$((pass + 1))
  printf '%-30s PASS\n' 'Persisted handover evidence'; pass=$((pass + 1))
  printf '%-30s PASS\n' 'Backup file and archive'; pass=$((pass + 1))
  printf '%-30s PASS\n' 'Recovery drill'; pass=$((pass + 1))
  printf '%-30s PASS\n' 'Reboot survival'; pass=$((pass + 1))
  printf '%-30s PASS\n' 'Service and backup timer'; pass=$((pass + 1))
  printf '%-30s PASS\n' 'Daily backup within 24h'; pass=$((pass + 1))
  printf '%-30s PASS\n' 'Retention timer and purge'; pass=$((pass + 1))
  printf '%-30s PASS\n' 'Job and definer roles'; pass=$((pass + 1))
  printf '%-30s PASS\n' 'Job unit hardening'; pass=$((pass + 1))
  if [[ $selected_count == 0 ]]; then
    printf '%-30s SKIP\n' 'External round-trip'; skip=$((skip + 1))
  else
    printf '%-30s PASS\n' 'External round-trip'; pass=$((pass + 1))
  fi
  printf '%-30s PASS\n' 'Revocation and evidence queries'; pass=$((pass + 1))
  printf 'Summary: PASS=%s FAIL=0 MANUAL=0 SKIP=%s\n' "$pass" "$skip"
  stage_mark c17 verified "C17 E handover read-back passed (backup, recovery, reboot, ${selected_count} selected integrations)"
}
