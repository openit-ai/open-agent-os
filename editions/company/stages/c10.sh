#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Sourced by install.sh. C10 closes G10 on the C02 E host.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/lib/stage.sh
. "$repo_root/editions/company/lib/stage.sh"

company_c10_env_get() {
  local file=$1 name=$2 line value='' found=0
  [[ -f $file ]] || return 1
  while IFS= read -r line; do
    case $line in
      "$name="*)
        value=${line#*=}
        found=$((found + 1)) ;;
    esac
  done < "$file"
  ((found == 1)) && [[ -n $value ]] || return 1
  printf '%s' "$value"
}

company_c10_put_env() {
  local file=$1 name=$2 value=$3
  [[ -n $value && $value != *$'\n'* && $value != *$'\r'* ]] || return 1
  printf '%s' "$value" | python3 "$repo_root/editions/company/app/env-key.py" "$file" "$name"
}

company_c10_validate_api() {
  local url=$1 token=$2 bot=$3 roles=${4:-system_admin}
  [[ $url =~ ^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~/-]*)?$ ||
     $url =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$ ]] || return 1
  [[ ${#token} -ge 8 && $token != *$'\n'* && $token != *$'\r'* ]] || return 1
  [[ $bot =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || return 1
  [[ $roles =~ ^[A-Za-z0-9_]+(\ [A-Za-z0-9_]+)*$ ]] || return 1
}

company_c10_apply() {
  local ready env_file config_dir
  local api_url api_token bot_id admin_roles secret_value slug setup_url
  local unit_dir app_dir
  # Migration 011 sits on top of 004-010; reaching it is only safe once the
  # console phase (C09, which requires C07 and its own dependencies) is verified.
  company_stage_ready c10 c02 c03 c09 || return $?
  [[ ${OAOS_COMPANY_HTTPS_ORIGIN:-} =~ ^https://[A-Za-z0-9.-]+$ ]] || {
    warn 'C10 BLOCKED: an existing HTTPS origin is required for the G10 link.'; return 3;
  }
  slug=${OAOS_COMPANY_ORG_SLUG:-company}
  [[ $slug =~ ^[a-z0-9][a-z0-9-]{1,62}$ ]] || {
    warn 'C10 BLOCKED: OAOS_COMPANY_ORG_SLUG must be a lowercase DNS-style slug.'; return 3;
  }

  config_dir="$HOME/.config/oaos-company"
  app_dir="$HOME/.local/lib/oaos-company"
  unit_dir="$HOME/.config/systemd/user"
  env_file="$config_dir/.env"
  [[ -f $env_file && $(stat -c %a "$env_file") == 600 ]] || {
    warn 'C10 BLOCKED: C02 Company EnvironmentFile is missing or not 0600.'; return 3;
  }

  # Project API values may come from the install environment or the existing
  # EnvironmentFile. They are never printed; the browser only sees the result.
  api_url=${OAOS_COMPANY_PROJECT_API_URL:-$(company_c10_env_get "$env_file" OAOS_COMPANY_PROJECT_API_URL || true)}
  api_token=${OAOS_COMPANY_PROJECT_API_TOKEN:-$(company_c10_env_get "$env_file" OAOS_COMPANY_PROJECT_API_TOKEN || true)}
  bot_id=${OAOS_COMPANY_PROJECT_BOT_USER_ID:-$(company_c10_env_get "$env_file" OAOS_COMPANY_PROJECT_BOT_USER_ID || true)}
  admin_roles=${OAOS_COMPANY_PROJECT_ADMIN_ROLES:-$(company_c10_env_get "$env_file" OAOS_COMPANY_PROJECT_ADMIN_ROLES || true)}
  admin_roles=${admin_roles:-system_admin}
  company_c10_validate_api "$api_url" "$api_token" "$bot_id" "$admin_roles" || {
    warn 'C10 BLOCKED: Project API URL, token, bot user ID and admin roles are required for G10.'
    return 3
  }

  company_stage_migrate c10 11 || return $?
  if ((COMPANY_STAGE_MIGRATED)); then
    cp -p -- "$env_file" "$COMPANY_STAGE_SNAPSHOT/company.env" || return 1
    [[ ! -f $unit_dir/oaos-company.service ]] ||
      cp -p -- "$unit_dir/oaos-company.service" "$COMPANY_STAGE_SNAPSHOT/oaos-company.service" || return 1
  fi

  # The console secret protects CSRF derivation only; codes and session tokens
  # are random per request and stored as SHA-256 hashes.
  if ! company_c10_env_get "$env_file" OAOS_COMPANY_CONSOLE_SECRET >/dev/null; then
    secret_value=$(python3 -c 'import base64,os; print(base64.b64encode(os.urandom(32)).decode("ascii"))') || return 1
    company_c10_put_env "$env_file" OAOS_COMPANY_CONSOLE_SECRET "$secret_value" || return 1
    unset secret_value
  fi
  company_c10_put_env "$env_file" OAOS_COMPANY_PROJECT_API_URL "$api_url" || return 1
  company_c10_put_env "$env_file" OAOS_COMPANY_PROJECT_API_TOKEN "$api_token" || return 1
  company_c10_put_env "$env_file" OAOS_COMPANY_PROJECT_BOT_USER_ID "$bot_id" || return 1
  company_c10_put_env "$env_file" OAOS_COMPANY_PROJECT_ADMIN_ROLES "$admin_roles" || return 1
  company_c10_put_env "$env_file" OAOS_COMPANY_ORG_SLUG "$slug" || return 1
  company_c10_put_env "$env_file" OAOS_COMPANY_HTTPS_ORIGIN "$OAOS_COMPANY_HTTPS_ORIGIN" || return 1
  unset api_token
  chmod 600 "$env_file"
  [[ $(stat -c %a "$env_file") == 600 && $(stat -c %U "$env_file") == "$(id -un)" ]] || {
    warn 'C10 EnvironmentFile permissions failed read-back.'; return 1;
  }

  install -d -m 700 "$app_dir" "$unit_dir" || return 1
  install -m 700 "$repo_root/editions/company/app/console_app.py" "$app_dir/console_app.py" || return 1
  install -m 700 "$repo_root/editions/company/app/project_api.py" "$app_dir/project_api.py" || return 1
  cat > "$unit_dir/oaos-company.service" <<EOF
[Unit]
Description=OAOS Company console, G10 setup and API
After=network.target

[Service]
Type=simple
EnvironmentFile=$config_dir/.env
ExecStart=/usr/bin/python3 $app_dir/console_app.py
Restart=on-failure
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=default.target
EOF
  chmod 600 "$unit_dir/oaos-company.service"
  systemctl --user daemon-reload || return 1
  systemctl --user restart oaos-company.service >/dev/null || return 1
  [[ $(systemctl --user is-enabled oaos-company.service 2>/dev/null || true) == enabled &&
     $(systemctl --user is-active oaos-company.service 2>/dev/null || true) == active ]] || {
    warn 'C10 Company console service did not become enabled and active.'; return 1;
  }
  ready=$(curl --silent --show-error --noproxy '*' --max-time 8 \
    http://127.0.0.1:8765/company/health 2>/dev/null || true)
  [[ ${ready//[$'\r\n']/} == ok ]] || {
    warn 'C10 Company console loopback health failed.'; return 1;
  }
  setup_url="${OAOS_COMPANY_HTTPS_ORIGIN}/company/setup"
  (umask 077; printf 'OAOS_COMPANY_SETUP_URL=%s\n' "$setup_url" > "$config_dir/console.conf") || return 1
  chmod 600 "$config_dir/console.conf"
  info 'C10 console and G10 setup URL applied; complete G10 in the HTTPS console and run read-back.'
}
