#!/usr/bin/env bash
# Sourced by install.sh. C11 configures the optional Slack adapter (G12).
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/stages/adapters.sh
. "$repo_root/editions/company/stages/adapters.sh"

company_c11_apply() { company_adapters_apply slack; }
