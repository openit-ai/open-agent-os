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
Usage: bash editions/company/install.sh --stage cNN [--status] [--help]
Stages: c01 through c17. c02-c17 are recognized but not implemented.
--status reads Company checkpoints without changing the system.
Exit: 0 = selected stage applied; 3 = prerequisite/gate blocked; 1 = error.
Database: set PGDATABASE and libpq PG* variables or PGSERVICE in the environment.
EOF
}

selected=''
show_status=0
while (($#)); do
  case $1 in
    --stage)
      (($# >= 2)) || { usage >&2; exit 1; }
      [[ -z $selected && $2 =~ ^c(0[1-9]|1[0-7])$ ]] || { usage >&2; exit 1; }
      selected=$2; shift 2 ;;
    --status) show_status=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
done
[[ -n ${HOME:-} ]] || { printf 'HOME is required.\n' >&2; exit 1; }
OAOS_STATE_FILE="$(oaos_home)/.oaos-install/company-state.json"
export OAOS_STATE_FILE

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
  stage_mark c01 blocked 'Project stack or verification is pending'
  warn 'Project stack and verification must be done first.'
  exit 3
fi

code=0
bash "$repo_root/editions/company/migrate.sh" || code=$?
case $code in
  0)
    if [[ $(stage_status c01) != verified ]]; then stage_mark c01 applied 'Migration checksum confirmed; read-back pending'; fi
    info 'c01 applied; run company-verify --phase c01 --read-back on E.' ;;
  3) stage_mark c01 blocked 'Database prerequisite unavailable'; warn 'c01 blocked.' ;;
  *) stage_mark c01 failed 'Migration failed; inspect database safely'; warn 'c01 failed.'; code=1 ;;
esac
show_table
exit "$code"
