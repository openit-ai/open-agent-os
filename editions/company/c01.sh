#!/usr/bin/env bash
# shellcheck disable=SC2034 # COMPANY_STAGE_NOTE is read by install.sh
# Sourced by install.sh. C01 creates the Company schema next to a verified
# Project installation; it runs as the libpq role before C02 separates the
# owner and runtime roles.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/lib/company.sh
. "$repo_root/editions/company/lib/company.sh"

# Project readiness: the Project edition state must show a finished stack and
# verification. This is the only place C01 reads the Project state namespace.
company_c01_project_ready() {
  local project_state project_ready
  project_state="$(oaos_home)/.oaos-install/state.json"
  if [[ ! -f $project_state ]]; then
    COMPANY_STAGE_NOTE='Project installation state is missing'
    warn 'Project installation state is missing.'
    return 3
  fi
  if have_cmd jq; then
    project_ready=$(jq -er '.edition == "project" and .stages.stack.status == "done" and .stages.verify.status == "done"' "$project_state" 2>/dev/null) || project_ready=false
  else
    project_ready=$(oaos_python - "$project_state" <<'PY' 2>/dev/null || true
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    state = json.load(f)
print(str(state.get('edition') == 'project' and
          state.get('stages', {}).get('stack', {}).get('status') == 'done' and
          state.get('stages', {}).get('verify', {}).get('status') == 'done').lower())
PY
)
  fi
  if [[ $project_ready != true ]]; then
    COMPANY_STAGE_NOTE='Project stack or verification is pending'
    warn 'Project stack and verification must be done first.'
    return 3
  fi
}

company_c01_apply() {
  local check_code=0 code=0 rollback_dir snapshot
  company_c01_project_ready || return $?
  # Like every later phase, C01 keeps a pre-migration dump of E as its rollback
  # point, taken through the local postgres maintenance account (no credential
  # is passed) into a 0700 rollback directory. Nothing is dumped when the
  # checksummed migration is already applied.
  bash "$repo_root/editions/company/migrate.sh" --check --through 1 >/dev/null 2>&1 || check_code=$?
  if ((check_code == 3)); then
    rollback_dir=${OAOS_COMPANY_ROLLBACK_DIR:-}
    if ! company_valid_path "$rollback_dir" || [[ ! -d $rollback_dir || ! -w $rollback_dir ||
          $(stat -c %a "$rollback_dir" 2>/dev/null) != 700 ]]; then
      COMPANY_STAGE_NOTE='Pre-migration backup directory is missing'
      warn 'Set OAOS_COMPANY_ROLLBACK_DIR to an existing 0700 directory for the C01 rollback dump.'
      return 3
    fi
    snapshot=$(mktemp -d "$rollback_dir/c01-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX") || die 'Cannot create the C01 rollback snapshot.'
    chmod 700 "$snapshot"
    if [[ ! ${PGDATABASE:-} =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || ! company_maint_socket_ok ||
       ! company_maint_dump "$snapshot/company.dump"; then
      COMPANY_STAGE_NOTE='Pre-migration E database backup failed'
      warn 'c01 blocked: the pre-migration E database backup failed or is empty.'
      return 3
    fi
  fi
  bash "$repo_root/editions/company/migrate.sh" --through 1 || code=$?
  case $code in
    0) info 'c01 applied; run company-verify --phase c01 --read-back on E.' ;;
    3) COMPANY_STAGE_NOTE='Database prerequisite unavailable'; warn 'c01 blocked.' ;;
    *) COMPANY_STAGE_NOTE='Migration failed; inspect database safely'; warn 'c01 failed.' ;;
  esac
  return "$code"
}
