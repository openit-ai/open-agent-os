#!/usr/bin/env bash
# Sourced by install.sh. C13 configures the optional Google Workspace adapter (G11).
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/adapters.sh
. "$repo_root/editions/company/adapters.sh"

company_c13_apply() { company_adapters_apply google_workspace; }
