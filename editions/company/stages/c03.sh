#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Sourced by install.sh. C03 uses the C02 E host and its dedicated account.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/lib/stage.sh
. "$repo_root/editions/company/lib/stage.sh"

company_c03_apply() {
  company_stage_apply c03 2 c01 c02 || return $?
  if ((COMPANY_STAGE_MIGRATED)); then info 'C03 migration applied; run E mapping read-back.'; fi
}
