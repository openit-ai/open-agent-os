#!/usr/bin/env bash
# Sourced by install.sh. C14 configures the optional Microsoft 365 adapter (G11).
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/adapters.sh
. "$repo_root/editions/company/adapters.sh"

company_c14_apply() { company_adapters_apply microsoft_365; }
