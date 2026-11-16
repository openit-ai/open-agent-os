#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Sourced by install.sh. C07 uses the C02 E host and the C04/C05/C06 identity boundary.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/lib/stage.sh
. "$repo_root/editions/company/lib/stage.sh"

company_c07_apply() {
  # Migrations are global and consecutive, so reaching version 8 also applies the
  # C11-C14 integration adapter schema from version 7 when it is still pending.
  # It carries no organization selection, key or configuration and is harmless
  # before the C11-C14 stages run.
  company_stage_apply c07 8 c01 c02 c03 c04 c05 c06 || return $?
  if ((COMPANY_STAGE_MIGRATED)); then info 'C07 policy, JIT approval and audit migration applied; run E policy read-back.'; fi
}
