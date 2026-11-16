#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
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
if ((OAOS_DRY_RUN)); then
  if [[ $selected == c11 || $selected == c12 || $selected == c13 || $selected == c14 ]]; then
    printf '%s\n' "Integration $selected plan: require C01-C05 plus the plan dependencies (C11: C06+C07, C12: C06-C08, C13/C14: C06) verified and C02 E host gates, check DB/RLS role, back up E DB, apply the integration adapter migration, select the organization's existing provider or SKIP, configure the organization app and read-only scopes, and store app credentials in the Company EnvironmentFile. Read-back records SKIP for an unselected provider and stays MANUAL until a real external round-trip is recorded. No changes made."
  elif [[ $selected == c15 ]]; then
    printf '%s\n' 'C15 plan: require C10 verified and C11-C14 verified or SKIP, close the G11/G12 gates, confirm the single productivity suite, remove SKIP provider keys from the EnvironmentFile and verify they keep no active credential. No changes made.'
  elif [[ $selected == c09 ]]; then
    printf '%s\n' 'C09 plan: require C01-C03 and C07 verified and C02 E host gates, check DB/RLS role, back up E DB, apply the console migration, install the loopback console service on the existing nginx /company/ path, then read back admin/member tabs, secret masking and service status. No changes made.'
  elif [[ $selected == c10 ]]; then
    printf '%s\n' 'C10 plan: require C02, C03 and C09 verified and C02 E host gates, check DB/RLS role, back up E DB, apply the console migration through 011, install the console with the HTTPS G10 setup link, keep the console secret and Project API values in the Company EnvironmentFile, then read back the HTTPS G10 link through the local TLS path, the setup/login SQL contract before G10 completes, and a real administrator login with two mappings afterwards. No changes made.'
  elif [[ $selected == c17 ]]; then
    printf '%s\n' 'C17 plan: require C01, C02, C10 and C15 verified and C16 verified or SKIP and C02 E host gates, check DB/RLS role, back up E DB, apply handover migration 012 and amendment 018, install the daily retention timer and run it once, create a verified backup and restore it into a scratch database, confirm systemd and reboot survival, require a confirmed external round-trip for each selected integration, and record the handover verdict and member revocation contract. No changes made.'
  elif [[ $selected == c16 ]]; then
    printf '%s\n' 'C16 plan: require C01-C05 and C07 verified and C02 E host gates, check DB/RLS role, back up E DB, apply LLM routing migration, configure the default model and optional G13 premium routes with a standard fallback (usage billing only through a C07 approval decided by a second named administrator), then read back routing, billing approval, cap and owner isolation; G13 not selected records SKIP. No changes made.'
  elif [[ $selected == c07 ]]; then
    printf '%s\n' 'C07 plan: require C01-C06 verified and C02 E host gates, check DB/RLS role, back up E DB, apply the policy/approval/audit migration, then read back default deny, explicit allow, admin-only one-time JIT approval with expiry and reuse denial, policy version invalidation, and the admin/member audit boundary. No changes made.'
  elif [[ $selected == c08 ]]; then
    printf '%s\n' 'C08 plan: require C01-C06 verified and C02 E host gates, check DB/RLS role, back up E DB, apply the ACL search migration, then read back personal/shared ACL search, cross-owner denial, ACL change/invalidation/expiry, deletion and audit without content. No changes made.'
  elif [[ $selected == c06 ]]; then
    printf '%s\n' 'C06 plan: require C01-C05 verified and C02 E host gates, check DB/RLS role, back up E DB, apply the identity isolation migration, then read back owner==credential==provider==output binding, cross-owner and provider denial, revocation and tenant isolation. No changes made.'
  elif [[ $selected == c05 ]]; then
    printf '%s\n' 'C05 plan: require C01-C04 verified and C02 E host gates, check DB/RLS role and AES-GCM, back up E DB and vault key, apply personal credential migration, then read back owner isolation, rotation and revocation. No changes made.'
  elif [[ $selected == c04 ]]; then
    printf '%s\n' 'C04 plan: require C01-C03 verified and C02 E host gates, check DB/RLS role, back up E DB, apply assistant migration, then read back two-owner isolation and revocation. No changes made.'
  elif [[ $selected == c03 ]]; then
    printf '%s\n' 'C03 plan: require C01/C02 verified and C02 E host gates, check DB/RLS role, back up E DB, apply mapping migration, then read back mapping decisions. No changes made.'
  elif [[ $selected == c02 ]]; then
    printf '%s\n' 'C02 plan: check E isolation, C01 verified, Ubuntu/systemd user manager, backup and rollback paths; create restricted environment and user units; add two nginx TLS paths; test/reload; read back linger, certificate and renewal timer. No changes made.'
  else
    printf 'Stage %s plan only; no changes made.\n' "$selected"
  fi
  exit 0
fi
if [[ $selected == c17 ]]; then
  # shellcheck source=editions/company/c17.sh
  . "$repo_root/editions/company/c17.sh"
  code=0
  company_c17_apply || code=$?
  case $code in
    0) if [[ $(stage_status c17) != verified ]]; then stage_mark c17 applied 'C17 backup, recovery, reboot and external evidence applied; E read-back pending'; fi ;;
    3) stage_mark c17 blocked 'C17 prerequisite, external round-trip, host or database gate pending' ;;
    *) stage_mark c17 failed 'C17 migration, backup or recovery drill failed; inspect E backup and schema'; code=1 ;;
  esac
  show_table
  exit "$code"
fi
if [[ $selected == c09 ]]; then
  # shellcheck source=editions/company/c09.sh
  . "$repo_root/editions/company/c09.sh"
  code=0
  company_c09_apply || code=$?
  case $code in
    0) if [[ $(stage_status c09) != verified ]]; then stage_mark c09 applied 'C09 console migration and service confirmed; E read-back pending'; fi ;;
    3) stage_mark c09 blocked 'C09 prerequisite, host or database gate pending' ;;
    *) stage_mark c09 failed 'C09 console migration or service installation failed; inspect E backup and schema'; code=1 ;;
  esac
  show_table
  exit "$code"
fi
if [[ $selected == c10 ]]; then
  # shellcheck source=editions/company/c10.sh
  . "$repo_root/editions/company/c10.sh"
  code=0
  company_c10_apply || code=$?
  case $code in
    0) if [[ $(stage_status c10) != verified ]]; then stage_mark c10 applied 'C10 console and G10 setup applied; E read-back pending'; fi ;;
    3) stage_mark c10 blocked 'C10 prerequisite, Project API, HTTPS origin or database gate pending' ;;
    *) stage_mark c10 failed 'C10 migration or console installation failed; inspect E backup and schema'; code=1 ;;
  esac
  show_table
  exit "$code"
fi
if [[ $selected == c16 ]]; then
  # shellcheck source=editions/company/c16.sh
  . "$repo_root/editions/company/c16.sh"
  code=0
  company_c16_apply || code=$?
  case $code in
    0) if [[ $(stage_status c16) != verified ]]; then stage_mark c16 applied 'C16 routing migration and G13 configuration confirmed; E read-back pending'; fi ;;
    3) stage_mark c16 blocked 'C16 prerequisite, G13 approval, host or database gate pending' ;;
    *) stage_mark c16 failed 'C16 migration or routing configuration failed; inspect E backup and schema'; code=1 ;;
  esac
  show_table
  exit "$code"
fi
if [[ $selected == c08 ]]; then
  # shellcheck source=editions/company/c08.sh
  . "$repo_root/editions/company/c08.sh"
  code=0
  company_c08_apply || code=$?
  case $code in
    0) if [[ $(stage_status c08) != verified ]]; then stage_mark c08 applied 'C08 ACL search migration checksum confirmed; E read-back pending'; fi ;;
    3) stage_mark c08 blocked 'C08 prerequisite, host or database gate pending' ;;
    *) stage_mark c08 failed 'C08 migration failed; inspect E backup and schema'; code=1 ;;
  esac
  show_table
  exit "$code"
fi
if [[ $selected == c07 ]]; then
  # shellcheck source=editions/company/c07.sh
  . "$repo_root/editions/company/c07.sh"
  code=0
  company_c07_apply || code=$?
  case $code in
    0) if [[ $(stage_status c07) != verified ]]; then stage_mark c07 applied 'C07 policy, JIT approval and audit migration checksum confirmed; E read-back pending'; fi ;;
    3) stage_mark c07 blocked 'C07 prerequisite, host or database gate pending' ;;
    *) stage_mark c07 failed 'C07 migration failed; inspect E backup and schema'; code=1 ;;
  esac
  show_table
  exit "$code"
fi
if [[ $selected == c06 ]]; then
  # shellcheck source=editions/company/c06.sh
  . "$repo_root/editions/company/c06.sh"
  code=0
  company_c06_apply || code=$?
  case $code in
    0) if [[ $(stage_status c06) != verified ]]; then stage_mark c06 applied 'C06 identity isolation migration checksum confirmed; E read-back pending'; fi ;;
    3) stage_mark c06 blocked 'C06 prerequisite, host or database gate pending' ;;
    *) stage_mark c06 failed 'C06 migration failed; inspect E backup and schema'; code=1 ;;
  esac
  show_table
  exit "$code"
fi
if [[ $selected == c05 ]]; then
  # shellcheck source=editions/company/c05.sh
  . "$repo_root/editions/company/c05.sh"
  code=0
  company_c05_apply || code=$?
  case $code in
    0) if [[ $(stage_status c05) != verified ]]; then stage_mark c05 applied 'C05 migration checksum and vault key confirmed; E credential read-back pending'; fi ;;
    3) stage_mark c05 blocked 'C05 prerequisite, host, vault or database gate pending' ;;
    *) stage_mark c05 failed 'C05 migration failed; inspect E backup and schema'; code=1 ;;
  esac
  show_table
  exit "$code"
fi
if [[ $selected == c04 ]]; then
  # shellcheck source=editions/company/c04.sh
  . "$repo_root/editions/company/c04.sh"
  code=0
  company_c04_apply || code=$?
  case $code in
    0) if [[ $(stage_status c04) != verified ]]; then stage_mark c04 applied 'C04 migration checksum confirmed; E assistant read-back pending'; fi ;;
    3) stage_mark c04 blocked 'C04 prerequisite, host or database gate pending' ;;
    *) stage_mark c04 failed 'C04 migration failed; inspect E backup and schema'; code=1 ;;
  esac
  show_table
  exit "$code"
fi
if [[ $selected == c03 ]]; then
  # shellcheck source=editions/company/c03.sh
  . "$repo_root/editions/company/c03.sh"
  code=0
  company_c03_apply || code=$?
  case $code in
    0) if [[ $(stage_status c03) != verified ]]; then stage_mark c03 applied 'C03 migration checksum confirmed; E mapping read-back pending'; fi ;;
    3) stage_mark c03 blocked 'C03 prerequisite, host or database gate pending' ;;
    *) stage_mark c03 failed 'C03 migration failed; inspect E backup and schema'; code=1 ;;
  esac
  show_table
  exit "$code"
fi
if [[ $selected == c02 ]]; then
  # shellcheck source=editions/company/c02.sh
  . "$repo_root/editions/company/c02.sh"
  code=0
  company_c02_apply || code=$?
  case $code in
    0) if [[ $(stage_status c02) != verified ]]; then stage_mark c02 applied 'C02 deployment resources applied; E read-back pending'; fi ;;
    3) stage_mark c02 blocked 'C02 prerequisite or safety gate pending' ;;
    *) stage_mark c02 failed 'C02 deployment failed; inspect rollback snapshot'; code=1 ;;
  esac
  show_table
  exit "$code"
fi
if [[ $selected =~ ^c1[1-5]$ ]]; then
  # shellcheck source=editions/company/c11.sh
  . "$repo_root/editions/company/$selected.sh"
  code=0
  "company_${selected}_apply" || code=$?
  case $code in
    0) if [[ $(stage_status "$selected") != verified ]]; then stage_mark "$selected" applied "$selected applied; E integration read-back pending"; fi ;;
    3) stage_mark "$selected" blocked "$selected prerequisite, selection or E host gate pending" ;;
    *) stage_mark "$selected" failed "$selected adapter configuration failed; inspect E backup and schema"; code=1 ;;
  esac
  show_table
  exit "$code"
fi
if [[ $selected != c01 ]]; then
  warn "$selected is recognized but not implemented."
  exit 3
fi

project_state="$(oaos_home)/.oaos-install/state.json"
if [[ ! -f $project_state ]]; then
  stage_mark c01 blocked 'Project installation state is missing'
  warn 'Project installation state is missing.'
  exit 3
fi
if have_cmd jq; then
  project_ready=$(jq -er '(if ((.editions // {}) | has("project")) then .editions.project.stages elif .edition == "project" then .stages else {} end) as $p | ($p.stack.status == "done" and $p.verify.status == "done")' "$project_state" 2>/dev/null) || project_ready=false
else
  project_ready=$(oaos_python - "$project_state" <<'PY' 2>/dev/null || true
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    state = json.load(f)
editions = state.get('editions') or {}
if 'project' in editions:
    stages = editions['project'].get('stages') or {}
elif state.get('edition') == 'project':
    stages = state.get('stages') or {}
else:
    stages = {}
print(str(stages.get('stack', {}).get('status') == 'done' and
          stages.get('verify', {}).get('status') == 'done').lower())
PY
)
fi
if [[ $project_ready != true ]]; then
  stage_mark c01 blocked 'Project stack or verification is pending'
  warn 'Project stack and verification must be done first.'
  exit 3
fi

# Like every later phase, C01 keeps a pre-migration dump of E as its rollback
# point, taken through the local postgres maintenance account (no credential
# is passed) into a 0700 rollback directory. Nothing is dumped when the
# checksummed migration is already applied.
check_code=0
bash "$repo_root/editions/company/migrate.sh" --check --through 1 >/dev/null 2>&1 || check_code=$?
if ((check_code == 3)); then
  rollback_dir=${OAOS_COMPANY_ROLLBACK_DIR:-}
  socket_dir=${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}
  if [[ ! $rollback_dir =~ ^/[A-Za-z0-9_./-]+$ || ! -d $rollback_dir || ! -w $rollback_dir ||
        $(stat -c %a "$rollback_dir" 2>/dev/null) != 700 ]]; then
    stage_mark c01 blocked 'Pre-migration backup directory is missing'
    warn 'Set OAOS_COMPANY_ROLLBACK_DIR to an existing 0700 directory for the C01 rollback dump.'
    show_table
    exit 3
  fi
  snapshot=$(mktemp -d "$rollback_dir/c01-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX") || die 'Cannot create the C01 rollback snapshot.'
  chmod 700 "$snapshot"
  if [[ ! ${PGDATABASE:-} =~ ^[A-Za-z_][A-Za-z0-9_]*$ || ! $socket_dir =~ ^/[A-Za-z0-9_./-]+$ ]] ||
     ! (umask 077; run_root runuser -u postgres -- env -u PGOPTIONS -u PGHOSTADDR \
        -u PGHOST -u PGUSER -u PGSERVICE -u PGPASSFILE -u PGPASSWORD \
        pg_dump --host="$socket_dir" --port="${PGPORT:-5432}" \
        --dbname="$PGDATABASE" --format=custom > "$snapshot/company.dump" 2>/dev/null) ||
     [[ ! -s $snapshot/company.dump ]]; then
    stage_mark c01 blocked 'Pre-migration E database backup failed'
    warn 'c01 blocked: the pre-migration E database backup failed or is empty.'
    show_table
    exit 3
  fi
  chmod 600 "$snapshot/company.dump"
fi
code=0
bash "$repo_root/editions/company/migrate.sh" --through 1 || code=$?
case $code in
  0)
    if [[ $(stage_status c01) != verified ]]; then stage_mark c01 applied 'Migration checksum confirmed; read-back pending'; fi
    info 'c01 applied; run company-verify --phase c01 --read-back on E.' ;;
  3) stage_mark c01 blocked 'Database prerequisite unavailable'; warn 'c01 blocked.' ;;
  *) stage_mark c01 failed 'Migration failed; inspect database safely'; warn 'c01 failed.'; code=1 ;;
esac
show_table
exit "$code"
