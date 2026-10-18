#!/usr/bin/env bash
# Sourced by install.sh. C15 closes the G11/G12 gates and checks consistency.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/adapters.sh
. "$repo_root/editions/company/adapters.sh"

company_c15_state() {
  local stage=$1
  case $(stage_status "$stage") in
    applied|verified) return 0 ;;
    SKIP) return 0 ;;
    *) return 1 ;;
  esac
}

company_c15_apply() {
  local org='' admin='' providers provider state selected_count=0 suite='' summary=''
  company_adapters_preflight slack || return $?
  for stage in c11 c12 c13 c14; do
    company_c15_state "$stage" || {
      warn "C15 BLOCKED: $stage must be applied or SKIP before the gate closes."; return 3;
    }
  done
  read -r org admin < <(company_adapters_admin_context) || true
  [[ -n $org && -n $admin ]] || {
    warn 'C15 BLOCKED: a single organization with an active G10 administrator is required.'; return 3;
  }
  company_adapters_migrate || return $?
  providers='slack notion google_workspace microsoft_365'
  for provider in $providers; do
    state=$(company_adapters_sql "$org" "$admin" \
      "SELECT company.integration_selection('$provider')") || return 1
    if [[ $state == selected ]]; then
      selected_count=$((selected_count + 1))
      case $provider in
        google_workspace|microsoft_365)
          [[ -z $suite ]] || {
            warn 'C15 BLOCKED: Google Workspace and Microsoft 365 cannot both be selected.'; return 3;
          }
          suite=$provider ;;
      esac
      check=$(company_adapters_sql "$org" "$admin" \
        "SELECT count(*) FROM company.integration_apps WHERE provider = '$provider' AND state = 'active'") || return 1
      [[ $check =~ ^[1-9][0-9]*$ ]] || {
        warn "C15 BLOCKED: selected provider $provider has no active organization app."; return 3;
      }
    else
      check=$(company_adapters_sql "$org" "$admin" \
        "SELECT count(*) FROM company.integration_apps WHERE provider = '$provider' AND state = 'active'
           UNION ALL SELECT count(*) FROM company.integration_delegations WHERE provider = '$provider' AND state = 'active'") || return 1
      [[ $check =~ ^0?$ ]] || {
        warn "C15 BLOCKED: SKIP provider $provider still has an active credential."; return 3;
      }
      summary+="$provider=SKIP "
    fi
  done
  info "C15 G11/G12 gates closed: selected=$selected_count ${suite:+suite=$suite }${summary}(existing services only; no new subscription)."
}
