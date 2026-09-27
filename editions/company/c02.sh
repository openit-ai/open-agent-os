#!/usr/bin/env bash
# Sourced by install.sh. C02 runs as the dedicated, unprivileged Company account.

company_c02_block() { warn "C02 BLOCKED: $1"; return 3; }
company_c02_valid_path() { [[ $1 =~ ^/[A-Za-z0-9_./-]+$ && $1 != / && $1 != *'//'* && $1 != *'/../'* && $1 != *'/./'* ]]; }
company_c02_write_vhost() {
  if [[ -w $1 ]]; then cp -- "$2" "$1"; else run_root cp -- "$2" "$1"; fi
}
company_c02_preflight() {
  local user id version host sockets account_home
  [[ $(uname -s) == Linux ]] || { company_c02_block 'Linux is required.'; return 3; }
  # C02 intentionally rejects unsupported distros instead of inheriting check_os's warning.
  [[ -r /etc/os-release ]] || { company_c02_block 'Ubuntu release information is missing.'; return 3; }
  id=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"')
  version=$(sed -n 's/^VERSION_ID=//p' /etc/os-release | tr -d '"')
  [[ $id == ubuntu && $version =~ ^(20\.04|22\.04|24\.04|26\.04)$ ]] || { company_c02_block 'Supported Ubuntu LTS is required.'; return 3; }
  ((EUID != 0)) || { company_c02_block 'Run as a dedicated non-root Company account.'; return 3; }
  user=$(id -un)
  [[ ${OAOS_COMPANY_USER:-} == "$user" ]] || { company_c02_block 'OAOS_COMPANY_USER must name the current dedicated account.'; return 3; }
  account_home=$(getent passwd "$user" | cut -d: -f6)
  [[ -n $account_home && $HOME == "$account_home" ]] || { company_c02_block 'HOME must be the Company account home used by systemd.'; return 3; }
  [[ ${OAOS_COMPANY_E_ID:-} =~ ^E-[A-Za-z0-9_-]+$ ]] || { company_c02_block 'Set an E instance identifier in OAOS_COMPANY_E_ID.'; return 3; }
  [[ $(stage_status c01) == verified ]] || { company_c02_block 'C01 must be verified on E.'; return 3; }
  [[ ${PGDATABASE:-} =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { company_c02_block 'Set E PGDATABASE to a simple database identifier.'; return 3; }
  [[ ${PGHOST:-127.0.0.1} == 127.0.0.1 || ${PGHOST:-} == localhost ]] || { company_c02_block 'PostgreSQL must use loopback.'; return 3; }
  [[ -z ${PGUSER:-} || ${PGUSER:-} =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { company_c02_block 'PGUSER must be a simple role identifier.'; return 3; }
  [[ ${OAOS_COMPANY_HTTPS_ORIGIN:-} =~ ^https://[A-Za-z0-9.-]+$ ]] || { company_c02_block 'Set an existing HTTPS origin.'; return 3; }
  local path
  for path in "${OAOS_COMPANY_BACKUP_DIR:-}" "${OAOS_COMPANY_KEY_BACKUP_DIR:-}" "${OAOS_COMPANY_ROLLBACK_DIR:-}" "${OAOS_COMPANY_NGINX_VHOST:-}"; do
    company_c02_valid_path "$path" || { company_c02_block 'Set absolute backup, key backup, rollback and nginx vhost paths.'; return 3; }
  done
  [[ -d $OAOS_COMPANY_BACKUP_DIR && -w $OAOS_COMPANY_BACKUP_DIR && -d $OAOS_COMPANY_KEY_BACKUP_DIR && -w $OAOS_COMPANY_KEY_BACKUP_DIR && -d $OAOS_COMPANY_ROLLBACK_DIR && -w $OAOS_COMPANY_ROLLBACK_DIR && -f $OAOS_COMPANY_NGINX_VHOST ]] || { company_c02_block 'Backup/rollback paths must exist and be writable; TLS vhost must exist.'; return 3; }
  [[ $OAOS_COMPANY_BACKUP_DIR != "$OAOS_COMPANY_KEY_BACKUP_DIR" && $OAOS_COMPANY_BACKUP_DIR != "$OAOS_COMPANY_ROLLBACK_DIR" && $OAOS_COMPANY_KEY_BACKUP_DIR != "$OAOS_COMPANY_ROLLBACK_DIR" ]] || { company_c02_block 'Backup, key backup and rollback need separate directories.'; return 3; }
  [[ $(stat -c %a "$OAOS_COMPANY_BACKUP_DIR") == 700 && $(stat -c %a "$OAOS_COMPANY_KEY_BACKUP_DIR") == 700 && $(stat -c %a "$OAOS_COMPANY_ROLLBACK_DIR") == 700 ]] || { company_c02_block 'Backup and rollback directories require mode 0700.'; return 3; }
  if ! { have_cmd systemctl && have_cmd loginctl && have_cmd nginx && have_cmd openssl && have_cmd pg_dump && have_cmd python3 && have_cmd ss && have_cmd curl; }; then
    company_c02_block 'Required Project host commands are missing.'; return 3
  fi
  systemctl --user show-environment >/dev/null 2>&1 || { company_c02_block 'systemd user manager is unavailable.'; return 3; }
  run_root true >/dev/null 2>&1 || { company_c02_block 'Non-interactive root access for nginx and linger is required.'; return 3; }
  sockets=$(ss -ltn 2>/dev/null) || { company_c02_block 'Cannot inspect listening sockets.'; return 3; }
  if awk '$1 == "LISTEN" && $4 ~ /:(5432|6379|8765)$/ && $4 !~ /^(127\.0\.0\.1|\[::1\]|::1):/ {bad=1} END {exit !bad}' <<< "$sockets"; then
    company_c02_block 'DB, Redis or Company port is publicly bound.'; return 3
  fi
  # Avoid sharing the existing gateway's user manager with the Company service.
  if systemctl --user list-unit-files hermes-gateway.service --no-legend 2>/dev/null | grep -Fq hermes-gateway.service; then
    company_c02_block 'Company account must be separate from the gateway account.'; return 3
  fi
  if ! { [[ $(systemctl is-enabled certbot.timer 2>/dev/null || true) == enabled && $(systemctl is-active certbot.timer 2>/dev/null || true) == active ]] || [[ $(systemctl is-enabled snap.certbot.renew.timer 2>/dev/null || true) == enabled && $(systemctl is-active snap.certbot.renew.timer 2>/dev/null || true) == active ]]; }; then
    company_c02_block 'Certificate renewal timer is not enabled and active.'; return 3
  fi
  host=${OAOS_COMPANY_HTTPS_ORIGIN#https://}
  run_root openssl x509 -checkend 2592000 -noout -in "/etc/letsencrypt/live/$host/fullchain.pem" >/dev/null 2>&1 || { company_c02_block 'TLS certificate is missing or expires within 30 days.'; return 3; }
  [[ -r /proc/sys/kernel/random/boot_id ]] || { company_c02_block 'Boot ID is unavailable.'; return 3; }
}

company_c02_apply() {
  company_c02_preflight || return 3
  local config_dir unit_dir app_dir stamp snapshot patched settings desired
  config_dir="$HOME/.config/oaos-company"
  unit_dir="$HOME/.config/systemd/user"
  app_dir="$HOME/.local/lib/oaos-company"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  snapshot=$(mktemp -d "$OAOS_COMPANY_ROLLBACK_DIR/c02-$stamp.XXXXXX") || return 1
  # A consistent pre-install E database dump and original TLS file are the rollback point.
  if [[ ! -e $snapshot/company.dump ]]; then
    pg_dump --format=custom --file="$snapshot/company.dump" "$PGDATABASE" >/dev/null 2>&1 || { warn 'Pre-install E database backup failed.'; return 3; }
    [[ -s $snapshot/company.dump ]] || { warn 'Pre-install E database backup is empty.'; return 3; }
  fi
  chmod 600 "$snapshot/company.dump"
  cp -- "$OAOS_COMPANY_NGINX_VHOST" "$snapshot/nginx.conf" || return 1
  [[ ! -f $config_dir/.env ]] || cp -p -- "$config_dir/.env" "$snapshot/company.env" || return 1
  [[ ! -f $(oaos_state_file) ]] || cp -p -- "$(oaos_state_file)" "$snapshot/company-state.json" || return 1
  local existing
  for existing in "$unit_dir/oaos-company.service" "$unit_dir/oaos-company-backup.service" "$unit_dir/oaos-company-backup.timer" "$app_dir/health.py" "$app_dir/backup.sh" "$config_dir/instance-id" "$config_dir/initial-boot-id" "$config_dir/deployment.conf"; do
    if [[ -e $existing ]]; then cp -p -- "$existing" "$snapshot/$(basename "$existing")" || return 1; fi
  done
  install -d -m 700 "$config_dir" "$app_dir" || return 1
  install -d -m 700 "$unit_dir" || return 1
  if [[ ! -e $config_dir/.env ]]; then
    (umask 077; printf 'PGDATABASE=%s\nPGHOST=%s\nOAOS_COMPANY_BACKUP_DIR=%s\nOAOS_COMPANY_KEY_BACKUP_DIR=%s\n' "$PGDATABASE" "${PGHOST:-127.0.0.1}" "$OAOS_COMPANY_BACKUP_DIR" "$OAOS_COMPANY_KEY_BACKUP_DIR" > "$config_dir/.env"; if [[ -n ${PGUSER:-} ]]; then printf 'PGUSER=%s\n' "$PGUSER" >> "$config_dir/.env"; fi) || return 1
  else
    grep -Fxq "PGDATABASE=$PGDATABASE" "$config_dir/.env" || { warn 'Existing Company environment targets a different database.'; return 3; }
  fi
  if [[ -e $config_dir/instance-id ]]; then
    [[ $(< "$config_dir/instance-id") == "$OAOS_COMPANY_E_ID" ]] || { warn 'Company environment belongs to another E instance.'; return 3; }
  else
    (umask 077; printf '%s\n' "$OAOS_COMPANY_E_ID" > "$config_dir/instance-id") || return 1
  fi
  settings="$config_dir/deployment.conf"
  desired=$(mktemp)
  printf 'OAOS_COMPANY_USER=%s\nOAOS_COMPANY_E_ID=%s\nPGDATABASE=%s\nPGHOST=%s\nPGUSER=%s\nOAOS_COMPANY_HTTPS_ORIGIN=%s\nOAOS_COMPANY_NGINX_VHOST=%s\nOAOS_COMPANY_BACKUP_DIR=%s\nOAOS_COMPANY_KEY_BACKUP_DIR=%s\nOAOS_COMPANY_ROLLBACK_DIR=%s\n' \
    "$OAOS_COMPANY_USER" "$OAOS_COMPANY_E_ID" "$PGDATABASE" "${PGHOST:-127.0.0.1}" "${PGUSER:-}" "$OAOS_COMPANY_HTTPS_ORIGIN" "$OAOS_COMPANY_NGINX_VHOST" "$OAOS_COMPANY_BACKUP_DIR" "$OAOS_COMPANY_KEY_BACKUP_DIR" "$OAOS_COMPANY_ROLLBACK_DIR" > "$desired"
  if [[ -e $settings ]]; then
    if ! cmp -s "$desired" "$settings"; then rm -f -- "$desired"; warn 'Existing E deployment settings differ; manual review required.'; return 3; fi
  else
    install -m 600 "$desired" "$settings" || { rm -f -- "$desired"; return 1; }
  fi
  rm -f -- "$desired"
  chmod 600 "$settings" "$config_dir/instance-id"
  chmod 700 "$config_dir"
  chmod 600 "$config_dir/.env"
  [[ $(stat -c %a "$config_dir") == 700 && $(stat -c %a "$config_dir/.env") == 600 && $(stat -c %U "$config_dir/.env") == "$(id -un)" ]] || { warn 'Company environment permissions failed read-back.'; return 1; }
  install -m 700 "$(dirname "${BASH_SOURCE[0]}")/health.py" "$app_dir/health.py" || return 1
  install -m 700 "$(dirname "${BASH_SOURCE[0]}")/backup.sh" "$app_dir/backup.sh" || return 1
  cat > "$unit_dir/oaos-company.service" <<EOF
[Unit]
Description=OAOS Company C02 health service (application added by later phases)
After=network.target

[Service]
Type=simple
EnvironmentFile=$config_dir/.env
ExecStart=/usr/bin/python3 $app_dir/health.py
Restart=on-failure
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=default.target
EOF
  cat > "$unit_dir/oaos-company-backup.service" <<EOF
[Unit]
Description=OAOS Company daily E backup

[Service]
Type=oneshot
EnvironmentFile=$config_dir/.env
ExecStart=$app_dir/backup.sh
UMask=0077
NoNewPrivileges=yes
EOF
  cat > "$unit_dir/oaos-company-backup.timer" <<'EOF'
[Unit]
Description=OAOS Company daily E backup timer

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
Unit=oaos-company-backup.service

[Install]
WantedBy=timers.target
EOF
  chmod 600 "$unit_dir/oaos-company.service" "$unit_dir/oaos-company-backup.service" "$unit_dir/oaos-company-backup.timer"
  systemctl --user daemon-reload || return 1
  systemctl --user enable --now oaos-company.service oaos-company-backup.timer >/dev/null || return 1
  for unit in oaos-company.service oaos-company-backup.timer; do
    [[ $(systemctl --user is-enabled "$unit") == enabled && $(systemctl --user is-active "$unit") == active ]] || { warn "$unit did not become enabled and active."; return 1; }
  done
  if [[ $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true) != yes ]]; then
    run_root loginctl enable-linger "$(id -un)" >/dev/null || return 3
  fi
  [[ $(loginctl show-user "$(id -un)" -p Linger --value) == yes ]] || { warn 'Linger enable read-back failed.'; return 3; }
  patched=$(mktemp)
  if ! python3 "$(dirname "${BASH_SOURCE[0]}")/nginx-paths.py" "$OAOS_COMPANY_NGINX_VHOST" "$OAOS_COMPANY_HTTPS_ORIGIN" > "$patched"; then rm -f -- "$patched"; return 3; fi
  if ! cmp -s "$patched" "$OAOS_COMPANY_NGINX_VHOST"; then
    company_c02_write_vhost "$OAOS_COMPANY_NGINX_VHOST" "$patched" || { rm -f -- "$patched"; return 1; }
    if ! run_root nginx -t >/dev/null 2>&1; then
      company_c02_write_vhost "$OAOS_COMPANY_NGINX_VHOST" "$snapshot/nginx.conf" || true
      rm -f -- "$patched"
      warn 'nginx syntax failed; original vhost restored.'
      return 1
    fi
    if ! run_root systemctl reload nginx >/dev/null; then
      company_c02_write_vhost "$OAOS_COMPANY_NGINX_VHOST" "$snapshot/nginx.conf" || true
      run_root systemctl reload nginx >/dev/null 2>&1 || true
      rm -f -- "$patched"
      warn 'nginx reload failed; original vhost restored.'
      return 1
    fi
  fi
  rm -f -- "$patched"
  if [[ $(curl --silent --show-error --noproxy '*' --max-time 8 --output /dev/null --write-out '%{http_code}' "$OAOS_COMPANY_HTTPS_ORIGIN/company/health" 2>/dev/null) != 200 ]]; then
    company_c02_write_vhost "$OAOS_COMPANY_NGINX_VHOST" "$snapshot/nginx.conf" || true
    run_root systemctl reload nginx >/dev/null 2>&1 || true
    warn 'Company HTTPS health failed; original vhost restored.'
    return 1
  fi
  # Certificate is read-only; renewal remains with the existing Project installation.
  local host cert
  host=${OAOS_COMPANY_HTTPS_ORIGIN#https://}
  cert="/etc/letsencrypt/live/$host/fullchain.pem"
  run_root openssl x509 -checkend 2592000 -noout -in "$cert" >/dev/null 2>&1 || { warn 'TLS certificate is missing or expires within 30 days.'; return 3; }
  if ! { [[ $(systemctl is-enabled certbot.timer 2>/dev/null || true) == enabled && $(systemctl is-active certbot.timer 2>/dev/null || true) == active ]] || [[ $(systemctl is-enabled snap.certbot.renew.timer 2>/dev/null || true) == enabled && $(systemctl is-active snap.certbot.renew.timer 2>/dev/null || true) == active ]]; }; then return 3; fi
  if [[ ! -e $config_dir/initial-boot-id ]]; then cp -- /proc/sys/kernel/random/boot_id "$config_dir/initial-boot-id"; chmod 600 "$config_dir/initial-boot-id"; fi
  info 'C02 resources applied. Run E read-back after a safe reboot; snapshot retained in rollback directory.'
}
