#!/usr/bin/env bash
# Sourced by install.sh. C09 uses the C02 E host and the C03/C07 identity and
# governance contracts.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# shellcheck source=editions/company/c02.sh
. "$repo_root/editions/company/c02.sh"
# shellcheck source=editions/company/c02-verify.sh
. "$repo_root/editions/company/c02-verify.sh"

company_c09_apply() {
  local ready check_code=0 role_safe snapshot socket_dir
  local config_dir unit_dir app_dir source_console unit_file console_port
  [[ $(stage_status c01) == verified && $(stage_status c02) == verified && \
     $(stage_status c03) == verified && $(stage_status c07) == verified ]] || {
    warn 'C09 BLOCKED: C01, C02, C03 and C07 must be verified on E.'; return 3;
  }
  company_c02_load_settings || { warn 'C09 BLOCKED: C02 E deployment settings are invalid.'; return 3; }
  company_c02_preflight || return 3
  export PGHOSTADDR=127.0.0.1
  if ! have_cmd psql || ! have_cmd pg_dump || ! have_cmd sha256sum || ! have_cmd python3; then
    warn 'C09 BLOCKED: PostgreSQL tools and Python are required.'; return 3
  fi
  export PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-5}
  ready=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT 1' 2>/dev/null) || {
    warn 'C09 BLOCKED: E database connection failed.'; return 3;
  }
  [[ $ready == 1 ]] || { warn 'C09 BLOCKED: E database did not answer.'; return 3; }
  role_safe=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c \
    'SELECT NOT (rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname = current_user' 2>/dev/null) || {
    warn 'C09 BLOCKED: database role check failed.'; return 3;
  }
  [[ $role_safe == t ]] || { warn 'C09 BLOCKED: database role bypasses RLS.'; return 3; }
  console_port=${OAOS_COMPANY_CONSOLE_PORT:-8765}
  [[ $console_port =~ ^[0-9]{4,5}$ ]] && ((console_port >= 1024 && console_port <= 65535)) || {
    warn 'C09 BLOCKED: OAOS_COMPANY_CONSOLE_PORT must be an unprivileged TCP port.'; return 3;
  }

  # Migrations are global and consecutive, so version 10 also applies the
  # earlier reserved C07/C08 slots and the C11 adapter schema when still pending.
  bash "$repo_root/editions/company/migrate.sh" --check --through 10 >/dev/null 2>&1 || check_code=$?
  case $check_code in
    0) ;;
    3)
      snapshot=$(mktemp -d "$OAOS_COMPANY_ROLLBACK_DIR/c09-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX") || return 1
      chmod 700 "$snapshot"
      socket_dir=${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}
      if ! company_c02_valid_path "$socket_dir" || [[ ! -d $socket_dir ]] ||
         [[ ! ${PGPORT:-5432} =~ ^[0-9]{1,5}$ ]]; then
        warn 'C09 BLOCKED: local PostgreSQL backup socket or port is invalid.'; return 3
      fi
      if ! (umask 077; run_root runuser -u postgres -- env -u PGOPTIONS -u PGHOSTADDR \
          -u PGHOST -u PGUSER -u PGSERVICE -u PGPASSFILE -u PGPASSWORD \
          pg_dump --host="$socket_dir" --port="${PGPORT:-5432}" \
          --dbname="$PGDATABASE" --format=custom > "$snapshot/company.dump" 2>/dev/null) ||
          [[ ! -s $snapshot/company.dump ]]; then
        warn 'C09 BLOCKED: E database backup failed or is empty.'; return 3
      fi
      chmod 600 "$snapshot/company.dump"
      bash "$repo_root/editions/company/migrate.sh" --through 10 || return 1 ;;
    *) warn 'C09 migration history or checksum is invalid.'; return 1 ;;
  esac

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
