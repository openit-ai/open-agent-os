#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Sourced by install.sh. C06 uses the C02 E host and the C04/C05 identity boundary.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/lib/stage.sh
. "$repo_root/editions/company/lib/stage.sh"

company_c06_apply() {
  # Migrations are global and consecutive, so reaching version 6 also applies the
  # C16/G13 schema from version 5 when it is still pending. It carries no keys or
  # configuration and is harmless before the C16 stage runs.
  company_stage_apply c06 6 c01 c02 c03 c04 c05 || return $?
  if ((COMPANY_STAGE_MIGRATED)); then info 'C06 identity isolation migration applied; run E identity read-back.'; fi
}
