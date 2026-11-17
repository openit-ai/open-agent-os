#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# Company runs log to ~/.oaos/logs/company-<date>.log, not the shared install log.
export OAOS_LOG_NAME=${OAOS_LOG_NAME:-company}
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"
export OAOS_STATE_EDITION=company
stages=()
for n in $(seq -w 1 17); do stages+=("c$n"); done
export OAOS_STAGES="${stages[*]}"

usage() {
  printf 'Usage: bash bootstrap/verify/company-verify.sh --phase cNN --read-back
C01 through C17 read-back implemented. Exit: 0 = passed; 3 = prerequisite/manual pending; 1 = failed.
'
}
phase=''
read_back=0
while (($#)); do
  case $1 in
    --phase)
      (($# >= 2)) || { usage >&2; exit 1; }
      [[ -z $phase && $2 =~ ^c(0[1-9]|1[0-7])$ ]] || { usage >&2; exit 1; }
      phase=$2; shift 2 ;;
    --read-back) read_back=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
done
[[ -n $phase && $read_back == 1 ]] || { usage >&2; exit 1; }
[[ -n ${HOME:-} ]] || { printf 'HOME is required.\n' >&2; exit 1; }
OAOS_STATE_FILE="$(oaos_home)/.oaos-install/company-state.json"
export OAOS_STATE_FILE
if ! state_valid; then die 'Company state file is invalid JSON.'; fi
# Phase table: C11-C14 share the adapter read-back per provider, C15 closes the
# integration gates, every other phase has editions/company/<phase>-verify.sh
# with company_<phase>_verify.
company_verify_phase() {
  case $phase in
    c1[1-4])
      # shellcheck source=editions/company/verify/adapters-verify.sh
      . "$repo_root/editions/company/verify/adapters-verify.sh"
      company_adapters_verify "$(company_adapters_stage "$phase")" "$phase" ;;
    c15)
      # shellcheck source=editions/company/verify/adapters-verify.sh
      . "$repo_root/editions/company/verify/adapters-verify.sh"
      company_adapters_verify_c15 ;;
    *)
      # shellcheck source=/dev/null
      . "$repo_root/editions/company/verify/$phase-verify.sh"
      "company_${phase}_verify" ;;
  esac
}
# Every read-back leaves one evidence JSON (company-design 9, O8): the rows
# are printed after the run, then lib/evidence.py writes
# ~/.oaos-install/evidence/company-<phase>-<UTC>.json (0700/0600) with the
# redacted output and its SHA-256. A failure to write evidence is reported but
# never changes the read-back exit code.
evidence_out=$(mktemp)
trap 'rm -f -- "$evidence_out"' EXIT
evidence_started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# The read-back keeps errexit (a subshell, not a || list), as before.
set +e
(set -e; company_verify_phase) > "$evidence_out"
evidence_code=$?
set -e
cat -- "$evidence_out"
if ! evidence_path=$(python3 "$repo_root/editions/company/lib/evidence.py" "$phase" \
    "$evidence_code" "$evidence_started" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$evidence_out" 2>/dev/null); then
  warn 'Read-back evidence JSON could not be written.'
else
  info "Evidence: $evidence_path"
fi
exit "$evidence_code"
