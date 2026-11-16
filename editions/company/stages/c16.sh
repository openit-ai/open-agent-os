#!/usr/bin/env bash
# Sourced by install.sh. C16 uses the C02 E host and C05 credential boundary.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/lib/stage.sh
. "$repo_root/editions/company/lib/stage.sh"

company_c16_identifier() { [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; }
company_c16_task() { [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; }

# Resolve an operator-named Project account to an active administrator of org.
company_c16_admin_member() {
  local org=$1 project_user_id=$2 value=''
  [[ $org =~ ^[0-9a-f-]{36}$ && $project_user_id =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || return 1
  value=$(company_maint_query "SELECT m.id FROM company.members m
         WHERE m.org_id = '$org'::uuid AND m.project_user_id = '$project_user_id'
           AND m.role = 'admin' AND m.state = 'active'") || return 1
  value=${value%%$'\n'*}
  [[ $value =~ ^[0-9a-f-]{36}$ ]] || return 1
  printf '%s' "$value"
}

# Usage billing is a C07 JIT approval: the requesting administrator asks, a
# different administrator named by the operator decides, the approval is
# consumed by the execution gate, and the model row is written in the same
# transaction (migration 016 rejects it otherwise and stores the approval id
# and the deciding administrator). Prints decision|model_id|approval_id.
company_c16_usage_model() {
  local org=$1 requester=$2 approver=$3 provider=$4 model=$5 cap=$6 output last
  output=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -v org="$org" -v requester="$requester" \
      -v approver="$approver" -v provider="$provider" -v model="$model" -v cap="$cap" \
      2>/dev/null <<'SQL'
BEGIN;
SELECT set_config('company.org_id', :'org', true) AS org_ctx \gset
SELECT set_config('company.member_id', :'requester', true) AS member_ctx \gset
-- The C07 bundle of the organization (idempotent) carries the llm.billing.usage rule.
SELECT r.decision AS bundle FROM company.install_default_policies() r \gset
SELECT company.g13_billing_digest(:'provider', :'model', :'cap'::numeric) AS digest \gset
SELECT COALESCE(r.approval_id::text, '') AS approval FROM company.request_approval(
  'llm.billing.usage', :'digest', 'g13') r \gset
SELECT set_config('company.member_id', :'approver', true) AS member_ctx \gset
SELECT r.decision AS decided FROM company.decide_approval(NULLIF(:'approval', '')::uuid, true) r \gset
SELECT set_config('company.member_id', :'requester', true) AS member_ctx \gset
SELECT r.decision AS gate FROM company.authorize_action('llm.billing.usage', :'digest', 'g13',
  NULLIF(:'approval', '')::uuid) r \gset
SELECT '__C16__' || r.decision || '|' || COALESCE(r.model_id::text, '') || '|' || :'approval'
  FROM company.configure_llm_model(:'provider', :'model', 'premium', 'usage', :'cap'::numeric, true) r;
COMMIT;
SQL
  ) || return 1
  last=${output##*__C16__}
  [[ $output == *__C16__* ]] || return 1
  printf '%s' "${last%%$'\n'*}"
}

company_c16_current_state() {
  local org=$1 value=''
  value=$(company_maint_query "SELECT g13_state FROM company.llm_settings WHERE org_id = '$org'::uuid") || return 1
  printf '%s' "${value%%$'\n'*}"
}

company_c16_current_default() {
  local org=$1 value=''
  value=$(company_maint_query "SELECT m.provider || '|' || m.model_name
          FROM company.llm_models m
          JOIN company.llm_routes r
            ON r.org_id = m.org_id AND r.primary_model_id = m.id
         WHERE r.org_id = '$org'::uuid AND r.task = 'default'
           AND m.tier = 'standard' AND m.state = 'active'
         LIMIT 1") || return 1
  printf '%s' "${value%%$'\n'*}"
}

company_c16_key_name() {
  printf '%s_API_KEY' "$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')"
}

company_c16_env_has_key() {
  local file=$1 name=$2
  grep -Eq "^${name}=.+" "$file" 2>/dev/null
}

company_c16_write_key() {
  local file=$1 name=$2
  printf '%s' "${OAOS_COMPANY_G13_API_KEY:-}" |
    python3 "$repo_root/editions/company/app/env-key.py" "$file" "$name"
}

company_c16_configure() {
  local org=$1 admin=$2 default_provider=$3 default_model=$4 state=$5
  local decision model_id premium_provider='' premium_model='' billing='flat' cap=0 approved=false
  local premium_decision premium_id task route_decision key_name apply_state=${OAOS_COMPANY_G13_STATE:+1}
  local requester_id='' approver_id=''
  local tasks task_list

  decision=$(company_sql "$org" "$admin" \
    "SELECT decision || '|' || model_id FROM company.configure_llm_model('$default_provider', '$default_model', 'standard', 'flat', 0, false)") || return 1
  [[ $decision == configured\|* ]] || { warn 'C16 BLOCKED: default model configuration was rejected.'; return 3; }
  model_id=${decision#*|}
  decision=$(company_sql "$org" "$admin" \
    "SELECT decision FROM company.set_llm_route('default', '$model_id', NULL)") || return 1
  [[ $decision == configured ]] || { warn 'C16 BLOCKED: default route configuration was rejected.'; return 3; }

  if [[ $state == selected && -z $apply_state ]]; then
    info 'C16 preserved the existing G13 selected configuration.'
    return 0
  fi
  if [[ $state != selected ]]; then
    decision=$(company_sql "$org" "$admin" \
      "SELECT company.set_llm_g13_state('skip', 'not_selected')") || return 1
    [[ $decision == configured ]] || { warn 'C16 BLOCKED: G13 SKIP state was rejected.'; return 3; }
    info 'C16 G13 SKIP(reason=not_selected); default model only.'
    return 0
  fi

  premium_provider=${OAOS_COMPANY_G13_PROVIDER:-}
  premium_model=${OAOS_COMPANY_G13_MODEL:-}
  billing=${OAOS_COMPANY_G13_BILLING_MODE:-flat}
  cap=${OAOS_COMPANY_G13_MONTHLY_CAP_USD:-0}
  company_c16_identifier "$premium_provider" && company_c16_identifier "$premium_model" || {
    warn 'C16 BLOCKED: Set OAOS_COMPANY_G13_PROVIDER and OAOS_COMPANY_G13_MODEL.'; return 3;
  }
  [[ $billing == flat || $billing == usage ]] || {
    warn 'C16 BLOCKED: G13 billing mode must be flat or usage.'; return 3;
  }
  [[ $billing != usage || $cap =~ ^[0-9]+([.][0-9]{1,2})?$ ]] || {
    warn 'C16 BLOCKED: usage billing requires a positive monthly cap.'; return 3;
  }
  if [[ $billing == usage ]]; then
    [[ $cap =~ ^[1-9][0-9]*([.][0-9]{1,2})?$ || $cap =~ ^0[.][0-9]*[1-9][0-9]*$ ]] || {
      warn 'C16 BLOCKED: usage billing requires a positive monthly cap.'; return 3;
    }
    # The approval is a C07 JIT decision by a named administrator, not a flag.
    requester_id=$(company_c16_admin_member "$org" "${OAOS_COMPANY_G13_REQUESTED_BY:-}") || {
      warn 'C16 BLOCKED: set OAOS_COMPANY_G13_REQUESTED_BY to the Project ID of the requesting active administrator.'; return 3;
    }
    approver_id=$(company_c16_admin_member "$org" "${OAOS_COMPANY_G13_APPROVED_BY:-}") || {
      warn 'C16 BLOCKED: set OAOS_COMPANY_G13_APPROVED_BY to the Project ID of the approving active administrator.'; return 3;
    }
    [[ $requester_id != "$approver_id" ]] || {
      warn 'C16 BLOCKED: the approving administrator must differ from the requester.'; return 3;
    }
    approved=true
  else
    cap=0
  fi
  key_name=$(company_c16_key_name "$premium_provider")
  [[ ${key_name} =~ ^[A-Z0-9_]+$ ]] || { warn 'C16 BLOCKED: provider key name is invalid.'; return 3; }
  if [[ -z ${OAOS_COMPANY_G13_API_KEY:-} ]] && ! company_c16_env_has_key "$HOME/.config/oaos-company/.env" "$key_name"; then
    warn 'C16 BLOCKED: G13 selected requires the provider API key in the Company EnvironmentFile.'
    return 3
  fi

  decision=$(company_sql "$org" "$admin" \
    "SELECT company.set_llm_g13_state('selected', 'operator_approved')") || return 1
  [[ $decision == configured ]] || { warn 'C16 BLOCKED: G13 selected state was rejected.'; return 3; }
  if [[ $billing == usage ]]; then
    premium_decision=$(company_c16_usage_model "$org" "$requester_id" "$approver_id" \
      "$premium_provider" "$premium_model" "$cap") || {
      warn 'C16 BLOCKED: the usage billing approval was not decided and consumed; nothing was stored.'; return 3;
    }
    [[ $premium_decision =~ ^configured\|[0-9a-f-]{36}\|[0-9a-f-]{36}$ ]] || {
      warn 'C16 BLOCKED: premium model configuration was rejected.'; return 3;
    }
    premium_id=${premium_decision#*|}
    info "C16 usage billing approval ${premium_id#*|} consumed and stored with the model."
    premium_id=${premium_id%%|*}
  else
    premium_decision=$(company_sql "$org" "$admin" \
      "SELECT decision || '|' || model_id FROM company.configure_llm_model('$premium_provider', '$premium_model', 'premium', '$billing', $cap, $approved)") || return 1
    [[ $premium_decision == configured\|* ]] || {
      warn 'C16 BLOCKED: premium model configuration was rejected.'; return 3;
    }
    premium_id=${premium_decision#*|}
  fi
  tasks=${OAOS_COMPANY_G13_TASKS:-premium}
  IFS=',' read -r -a task_list <<< "$tasks"
  for task in "${task_list[@]}"; do
    company_c16_task "$task" || { warn 'C16 BLOCKED: G13 task name is invalid.'; return 3; }
    route_decision=$(company_sql "$org" "$admin" \
      "SELECT decision FROM company.set_llm_route('$task', '$premium_id', '$model_id')") || return 1
    [[ $route_decision == configured ]] || {
      warn "C16 BLOCKED: route $task was rejected."; return 3;
    }
  done
  if [[ -n ${OAOS_COMPANY_G13_API_KEY:-} ]]; then
    company_c16_write_key "$HOME/.config/oaos-company/.env" "$key_name" || {
      warn 'C16 BLOCKED: cannot store the G13 provider key safely.'; return 3;
    }
  fi
  chmod 600 "$HOME/.config/oaos-company/.env" || return 1
  info 'C16 G13 selected: premium routes configured with standard fallback; key stored in Company EnvironmentFile.'
}

company_c16_apply() {
  local org='' admin='' current_state=''
  local default_provider default_model current_default state
  company_stage_ready c16 c01 c02 c03 c04 c05 c07 || return $?
  read -r org admin < <(company_admin_context) || {
    warn 'C16 BLOCKED: a single organization with an active G10 administrator is required.'; return 3;
  }
  [[ -n $org && -n $admin ]] || {
    warn 'C16 BLOCKED: a single organization with an active G10 administrator is required.'; return 3;
  }
  company_stage_migrate c16 8 || return $?

  current_state=$(company_c16_current_state "$org") || current_state=''
  state=${OAOS_COMPANY_G13_STATE:-$current_state}
  [[ -n $state ]] || state=skip
  [[ $state == skip || $state == selected ]] || {
    warn 'C16 BLOCKED: OAOS_COMPANY_G13_STATE must be skip or selected.'; return 3;
  }

  default_provider=${OAOS_COMPANY_DEFAULT_PROVIDER:-}
  default_model=${OAOS_COMPANY_DEFAULT_MODEL:-}
  if [[ -z $default_provider || -z $default_model ]]; then
    current_default=$(company_c16_current_default "$org") || current_default=''
    default_provider=${current_default%%|*}
    default_model=${current_default#*|}
  fi
  if ! company_c16_identifier "$default_provider" || ! company_c16_identifier "$default_model"; then
    warn 'C16 BLOCKED: set OAOS_COMPANY_DEFAULT_PROVIDER and OAOS_COMPANY_DEFAULT_MODEL from G1.'
    return 3
  fi

  company_c16_configure "$org" "$admin" "$default_provider" "$default_model" "$state" || return $?
  info 'C16 routing contract applied; run the E read-back.'
}
