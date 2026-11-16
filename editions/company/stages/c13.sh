#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Sourced by install.sh. C13 configures the optional Google Workspace adapter (G11).
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/stages/adapters.sh
. "$repo_root/editions/company/stages/adapters.sh"

company_c13_apply() { company_adapters_apply google_workspace; }
