#!/usr/bin/env bash
# Sourced by install.sh. C08 uses the C02 E host and the C04/C06 identity boundary.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/lib/stage.sh
. "$repo_root/editions/company/lib/stage.sh"

company_c08_apply() {
  company_stage_apply c08 9 c01 c02 c03 c04 c05 c06 || return $?
  if ((COMPANY_STAGE_MIGRATED)); then info 'C08 ACL search migration applied; run E search read-back.'; fi
}
