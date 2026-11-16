#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Common C03+ phase skeleton. Sourced by the phase scripts after
# bootstrap/lib/common.sh. Every database phase runs the same sequence:
#   1. required phases verified on E,
#   2. C02 deployment settings and E host preflight,
#   3. PostgreSQL tools, connection and a runtime role that cannot bypass RLS,
#      plus the backup/maintenance job roles (company_db_jobs_ensure),
#   4. migrate.sh --check; when pending, a maintenance-account pg_dump into a
#      fresh 0700 rollback snapshot, then migrate.sh through the phase version.
# Exit codes follow install.sh: 0 = done, 3 = blocked, 1 = failed.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/lib/company.sh
. "$repo_root/editions/company/lib/company.sh"
# shellcheck source=editions/company/stages/c02.sh
. "$repo_root/editions/company/stages/c02.sh"
# shellcheck source=editions/company/verify/c02-verify.sh
. "$repo_root/editions/company/verify/c02-verify.sh"

# Message prefix: "C03" for a phase, "Integration" for the C11-C14 adapters.
company_stage_label() {
  case $1 in
    adapters) printf 'Integration' ;;
    *) printf '%s' "$1" | tr '[:lower:]' '[:upper:]' ;;
  esac
}
company_stage_block() { warn "$(company_stage_label "$1") BLOCKED: $2"; return 3; }

# company_stage_ready <phase> [required phase...]
company_stage_ready() {
  local phase=$1 dep ready role_safe
  shift
  for dep in "$@"; do
    [[ $(stage_status "$dep") == verified ]] || {
      company_stage_block "$phase" "$(company_phase_list "$@") must be verified on E."; return 3;
    }
  done
  company_c02_load_settings || { company_stage_block "$phase" 'C02 E deployment settings are invalid.'; return 3; }
  company_c02_preflight || return 3
  export PGHOSTADDR=127.0.0.1 PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  if ! have_cmd psql || ! have_cmd pg_dump || ! have_cmd sha256sum || ! have_cmd python3; then
    company_stage_block "$phase" 'PostgreSQL tools and Python are required.'; return 3
  fi
  ready=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' 2>/dev/null) || {
    company_stage_block "$phase" 'E database connection failed.'; return 3;
  }
  [[ $ready == 1 ]] || { company_stage_block "$phase" 'E database did not answer.'; return 3; }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    company_stage_block "$phase" 'database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { company_stage_block "$phase" 'database role bypasses RLS.'; return 3; }
  # Backup/maintenance job roles and their 0600 password file (O7).
  company_db_jobs_ensure || {
    company_stage_block "$phase" 'database job roles or their password file could not be prepared.'; return 3;
  }
}

# company_stage_migrate <phase> <through>
# Sets COMPANY_STAGE_MIGRATED=1 when migrations were applied, 0 when the
# recorded checksums already cover <through>; COMPANY_STAGE_SNAPSHOT names the
# rollback snapshot of an applied run.
# shellcheck disable=SC2034 # read by the sourcing phase scripts
COMPANY_STAGE_MIGRATED=0
COMPANY_STAGE_SNAPSHOT=''
company_stage_migrate() {
  local phase=$1 through=$2 check_code=0 snapshot label
  label=$(company_stage_label "$phase")
  # shellcheck disable=SC2034 # read by the sourcing phase scripts
  COMPANY_STAGE_MIGRATED=0 COMPANY_STAGE_SNAPSHOT=''
  bash "$repo_root/editions/company/lib/migrate.sh" --check --through "$through" >/dev/null 2>&1 || check_code=$?
  case $check_code in
    0) info "$label migration checksum confirmed; no database change needed."; return 0 ;;
    3) ;;
    *) warn "$label migration history or checksum is invalid."; return 1 ;;
  esac
  company_maint_socket_ok || {
    company_stage_block "$phase" 'local PostgreSQL backup socket or port is invalid.'; return 3;
  }
  snapshot=$(mktemp -d "$OAOS_COMPANY_ROLLBACK_DIR/$phase-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX") || return 1
  chmod 700 "$snapshot"
  company_maint_dump "$snapshot/company.dump" || {
    company_stage_block "$phase" 'E database backup failed or is empty.'; return 3;
  }
  bash "$repo_root/editions/company/lib/migrate.sh" --through "$through" || return 1
  # shellcheck disable=SC2034 # read by the sourcing phase scripts
  COMPANY_STAGE_MIGRATED=1 COMPANY_STAGE_SNAPSHOT=$snapshot
}

# company_stage_apply <phase> <through> [required phase...]
company_stage_apply() {
  local phase=$1 through=$2
  shift 2
  company_stage_ready "$phase" "$@" || return $?
  company_stage_migrate "$phase" "$through"
}
