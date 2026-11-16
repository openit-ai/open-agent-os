#!/usr/bin/env bash
# Sourced by install.sh. C04 uses the C02 E host and its dedicated account.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/lib/stage.sh
. "$repo_root/editions/company/lib/stage.sh"

company_c04_apply() {
  company_stage_apply c04 3 c01 c02 c03 || return $?
  if ((COMPANY_STAGE_MIGRATED)); then info 'C04 migration applied; run E assistant read-back.'; fi
}
