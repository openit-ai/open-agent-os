#!/usr/bin/env bash
# Sourced by install.sh. C09 uses the C02 E host and the C03/C07 identity and
# governance contracts.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/lib/stage.sh
. "$repo_root/editions/company/lib/stage.sh"

company_c09_apply() {
  local config_dir unit_dir app_dir source_console unit_file console_port
  company_stage_ready c09 c01 c02 c03 c07 || return $?
  console_port=${OAOS_COMPANY_CONSOLE_PORT:-8765}
  [[ $console_port =~ ^[0-9]{4,5}$ ]] && ((console_port >= 1024 && console_port <= 65535)) || {
    warn 'C09 BLOCKED: OAOS_COMPANY_CONSOLE_PORT must be an unprivileged TCP port.'; return 3;
  }

  # Migrations are global and consecutive, so version 10 also applies the
  # earlier reserved C07/C08 slots and the C11 adapter schema when still pending.
  company_stage_migrate c09 10 || return $?

  config_dir="$HOME/.config/oaos-company"
  unit_dir="$HOME/.config/systemd/user"
  app_dir="$HOME/.local/lib/oaos-company"
  install -d -m 700 "$app_dir" || return 1
  install -d -m 700 "$unit_dir" || return 1
  source_console="$(dirname "${BASH_SOURCE[0]}")/console.py"
  install -m 700 "$source_console" "$app_dir/console.py" || return 1

  unit_file="$unit_dir/oaos-company.service"
  cat > "$unit_file" <<EOF
[Unit]
Description=OAOS Company C09 management console
After=network.target

[Service]
Type=simple
EnvironmentFile=$config_dir/.env
Environment=OAOS_COMPANY_CONSOLE_PORT=$console_port
ExecStart=/usr/bin/python3 $app_dir/console.py
Restart=on-failure
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=default.target
EOF
  chmod 600 "$unit_file" || return 1
  systemctl --user daemon-reload || return 1
  systemctl --user enable --now oaos-company.service >/dev/null || return 1
  [[ $(systemctl --user is-enabled oaos-company.service 2>/dev/null || true) == enabled ]] || {
    warn 'C09 service did not become enabled.'; return 1;
  }
  [[ $(systemctl --user is-active oaos-company.service 2>/dev/null || true) == active ]] || {
    warn 'C09 console service did not become active.'; return 1;
  }
  info "C09 management console applied on 127.0.0.1:$console_port; run the E read-back."
}
