#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# Company runs log to ~/.oaos/logs/company-<date>.log, not the shared install log.
export OAOS_LOG_NAME=${OAOS_LOG_NAME:-company}
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"

stages=()
for n in $(seq -w 1 17); do stages+=("c$n"); done
export OAOS_STATE_EDITION=company OAOS_STAGES="${stages[*]}"

usage() {
  cat <<'EOF'
Usage: bash editions/company/install.sh --stage cNN [--dry-run] [--status] [--help]
Stages: c01 through c17. All stages are implemented.
--status reads Company checkpoints without changing the system.
--dry-run prints the selected stage plan without changing state or the system.
Exit: 0 = selected stage applied; 3 = prerequisite/gate blocked; 1 = error.
Database: set PGDATABASE and libpq PG* variables or PGSERVICE in the environment.
EOF
}

selected=''
show_status=0
OAOS_DRY_RUN=0
while (($#)); do
  case $1 in
    --stage)
      (($# >= 2)) || { usage >&2; exit 1; }
      [[ -z $selected && $2 =~ ^c(0[1-9]|1[0-7])$ ]] || { usage >&2; exit 1; }
      selected=$2; shift 2 ;;
    --status) show_status=1; shift ;;
    --dry-run) OAOS_DRY_RUN=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
done
[[ -n ${HOME:-} ]] || { printf 'HOME is required.\n' >&2; exit 1; }
OAOS_STATE_FILE="$(oaos_home)/.oaos-install/company-state.json"
export OAOS_STATE_FILE
if ((OAOS_DRY_RUN)); then export OAOS_NO_LOG_FILE=1; fi

show_table() {
  local stage status time
  printf '%-6s %-10s %s\n' PHASE STATUS VERIFIED_AT
  for stage in "${stages[@]}"; do
    status=$(stage_status "$stage")
    time='-'
    if [[ $status == verified ]]; then
      if have_cmd jq; then
        time=$(jq -r --arg name "$stage" '.stages[$name].updated_at // "-"' "$(oaos_state_file)")
      else
        time=$(oaos_python - "$(oaos_state_file)" "$stage" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    state = json.load(f)
print(state.get('stages', {}).get(sys.argv[2], {}).get('updated_at', '-'))
PY
)
      fi
    fi
    printf '%-6s %-10s %s\n' "$stage" "$status" "$time"
  done
}

if ! state_valid; then die 'Company state file is invalid JSON; preserve and repair it before retrying.'; fi
if ((show_status)); then show_table; exit 0; fi
[[ -n $selected ]] || { usage >&2; exit 1; }
# Phase table: dry-run plan, then the checkpoint notes for applied (0),
# blocked (3) and failed (other) results of company_<phase>_apply.
company_phase_plan() {
  local selected=$1
  case $selected in
    c11|c12|c13|c14) printf '%s\n' "Integration $selected plan: require C01-C05 plus the plan dependencies (C11: C06+C07, C12: C06-C08, C13/C14: C06) verified and C02 E host gates, check DB/RLS role, back up E DB, apply the integration adapter migration, select the organization's existing provider or SKIP, configure the organization app and read-only scopes, and store app credentials in the Company EnvironmentFile. Read-back records SKIP for an unselected provider and stays MANUAL until a real external round-trip is recorded. No changes made." ;;
    c15) printf '%s\n' 'C15 plan: require C10 verified and C11-C14 verified or SKIP, close the G11/G12 gates, confirm the single productivity suite, remove SKIP provider keys from the EnvironmentFile and verify they keep no active credential. No changes made.' ;;
    c09) printf '%s\n' 'C09 plan: require C01-C03 and C07 verified and C02 E host gates, check DB/RLS role, back up E DB, apply the console migration, install the loopback console service on the existing nginx /company/ path, then read back admin/member tabs, secret masking and service status. No changes made.' ;;
    c10) printf '%s\n' 'C10 plan: require C02, C03 and C09 verified and C02 E host gates, check DB/RLS role, back up E DB, apply the console migration through 011, install the console with the HTTPS G10 setup link, keep the console secret and Project API values in the Company EnvironmentFile, then read back the HTTPS G10 link through the local TLS path, the setup/login SQL contract before G10 completes, and a real administrator login with two mappings afterwards. No changes made.' ;;
    c17) printf '%s\n' 'C17 plan: require C01, C02, C10 and C15 verified and C16 verified or SKIP and C02 E host gates, check DB/RLS role, back up E DB, apply handover migration 012 and amendment 018, install the daily retention timer and run it once, create a verified backup, restore it and the newest vault key backup into a scratch database, re-run the assistant isolation, audit and ACL search read-backs there and record the measured recovery time (failing above the 4-hour RTO), confirm systemd and reboot survival, require a confirmed external round-trip for each selected integration, and record the handover verdict and member revocation contract. No changes made.' ;;
    c16) printf '%s\n' 'C16 plan: require C01-C05 and C07 verified and C02 E host gates, check DB/RLS role, back up E DB, apply LLM routing migration, configure the default model and optional G13 premium routes with a standard fallback (usage billing only through a C07 approval decided by a second named administrator), then read back routing, billing approval, cap and owner isolation; G13 not selected records SKIP. No changes made.' ;;
    c07) printf '%s\n' 'C07 plan: require C01-C06 verified and C02 E host gates, check DB/RLS role, back up E DB, apply the policy/approval/audit migration, then read back default deny, explicit allow, admin-only one-time JIT approval with expiry and reuse denial, policy version invalidation, and the admin/member audit boundary. No changes made.' ;;
    c08) printf '%s\n' 'C08 plan: require C01-C06 verified and C02 E host gates, check DB/RLS role, back up E DB, apply the ACL search migration, then read back personal/shared ACL search, cross-owner denial, ACL change/invalidation/expiry, deletion and audit without content. No changes made.' ;;
    c06) printf '%s\n' 'C06 plan: require C01-C05 verified and C02 E host gates, check DB/RLS role, back up E DB, apply the identity isolation migration, then read back owner==credential==provider==output binding, cross-owner and provider denial, revocation and tenant isolation. No changes made.' ;;
    c05) printf '%s\n' 'C05 plan: require C01-C04 verified and C02 E host gates, check DB/RLS role and AES-GCM, back up E DB and vault key, apply personal credential migration, then read back owner isolation, rotation and revocation. No changes made.' ;;
    c04) printf '%s\n' 'C04 plan: require C01-C03 verified and C02 E host gates, check DB/RLS role, back up E DB, apply assistant migration, then read back two-owner isolation and revocation. No changes made.' ;;
    c03) printf '%s\n' 'C03 plan: require C01/C02 verified and C02 E host gates, check DB/RLS role, back up E DB, apply mapping migration, then read back mapping decisions. No changes made.' ;;
    c02) printf '%s\n' 'C02 plan: check E isolation, C01 verified, Ubuntu/systemd user manager, backup and rollback paths; create restricted environment and user units; add two nginx TLS paths; test/reload; read back linger, certificate and renewal timer. No changes made.' ;;
    *) printf 'Stage %s plan only; no changes made.\n' "$selected" ;;
  esac
}
company_phase_notes() {
  case $1 in
    c01) printf '%s\n' 'Migration checksum confirmed; read-back pending' 'Database prerequisite unavailable' 'Migration failed; inspect database safely' ;;
    c02) printf '%s\n' 'C02 deployment resources applied; E read-back pending' 'C02 prerequisite or safety gate pending' 'C02 deployment failed; inspect rollback snapshot' ;;
    c03) printf '%s\n' 'C03 migration checksum confirmed; E mapping read-back pending' 'C03 prerequisite, host or database gate pending' 'C03 migration failed; inspect E backup and schema' ;;
    c04) printf '%s\n' 'C04 migration checksum confirmed; E assistant read-back pending' 'C04 prerequisite, host or database gate pending' 'C04 migration failed; inspect E backup and schema' ;;
    c05) printf '%s\n' 'C05 migration checksum and vault key confirmed; E credential read-back pending' 'C05 prerequisite, host, vault or database gate pending' 'C05 migration failed; inspect E backup and schema' ;;
    c06) printf '%s\n' 'C06 identity isolation migration checksum confirmed; E read-back pending' 'C06 prerequisite, host or database gate pending' 'C06 migration failed; inspect E backup and schema' ;;
    c07) printf '%s\n' 'C07 policy, JIT approval and audit migration checksum confirmed; E read-back pending' 'C07 prerequisite, host or database gate pending' 'C07 migration failed; inspect E backup and schema' ;;
    c08) printf '%s\n' 'C08 ACL search migration checksum confirmed; E read-back pending' 'C08 prerequisite, host or database gate pending' 'C08 migration failed; inspect E backup and schema' ;;
    c09) printf '%s\n' 'C09 console migration and service confirmed; E read-back pending' 'C09 prerequisite, host or database gate pending' 'C09 console migration or service installation failed; inspect E backup and schema' ;;
    c10) printf '%s\n' 'C10 console and G10 setup applied; E read-back pending' 'C10 prerequisite, Project API, HTTPS origin or database gate pending' 'C10 migration or console installation failed; inspect E backup and schema' ;;
    c1[1-5]) printf '%s\n' "$1 applied; E integration read-back pending" "$1 prerequisite, selection or E host gate pending" "$1 adapter configuration failed; inspect E backup and schema" ;;
    c16) printf '%s\n' 'C16 routing migration and G13 configuration confirmed; E read-back pending' 'C16 prerequisite, G13 approval, host or database gate pending' 'C16 migration or routing configuration failed; inspect E backup and schema' ;;
    c17) printf '%s\n' 'C17 backup, recovery, reboot and external evidence applied; E read-back pending' 'C17 prerequisite, external round-trip, host or database gate pending' 'C17 migration, backup or recovery drill failed; inspect E backup and schema' ;;
  esac
}

if ((OAOS_DRY_RUN)); then
  company_phase_plan "$selected"
  exit 0
fi

# Every phase script defines company_<phase>_apply (0 = done, 3 = blocked,
# other = failed) and may set COMPANY_STAGE_NOTE to a more specific note.
# shellcheck source=/dev/null
. "$repo_root/editions/company/stages/$selected.sh"
COMPANY_STAGE_NOTE=''
code=0
"company_${selected}_apply" || code=$?
notes=()
while IFS= read -r line; do notes+=("$line"); done < <(company_phase_notes "$selected")
case $code in
  0) if [[ $(stage_status "$selected") != verified ]]; then stage_mark "$selected" applied "${notes[0]}"; fi ;;
  3) stage_mark "$selected" blocked "${COMPANY_STAGE_NOTE:-${notes[1]}}" ;;
  *) stage_mark "$selected" failed "${COMPANY_STAGE_NOTE:-${notes[2]}}"; code=1 ;;
esac
show_table
exit "$code"
