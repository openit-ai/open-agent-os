#!/usr/bin/env bash
# Sourced by install.sh. C12 configures the optional Notion adapter (G12).
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/stages/adapters.sh
. "$repo_root/editions/company/stages/adapters.sh"

company_c12_apply() { company_adapters_apply notion; }
