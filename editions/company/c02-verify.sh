#!/usr/bin/env bash
# Sourced by company-verify.sh. Never prints EnvironmentFile values.

company_c02_check() {
  local label=$1
  shift
  if "$@"; then
    printf '%-27s PASS\n' "$label"
    pass=$((pass + 1))
  else
    printf '%-27s FAIL\n' "$label"
    fail=$((fail + 1))
  fi
}
company_c02_load_settings() {
  local file key value seen='' count=0
  file="$HOME/.config/oaos-company/deployment.conf"
  [[ -f $file && $(stat -c %a "$file") == 600 && $(stat -c %U "$file") == "$(id -un)" ]] || return 1
  while IFS='=' read -r key value; do
    case $key in
      OAOS_COMPANY_USER|OAOS_COMPANY_E_ID|PGDATABASE|PGHOST|PGUSER|OAOS_COMPANY_HTTPS_ORIGIN|OAOS_COMPANY_NGINX_VHOST|OAOS_COMPANY_BACKUP_DIR|OAOS_COMPANY_KEY_BACKUP_DIR|OAOS_COMPANY_ROLLBACK_DIR) ;;
      *) return 1 ;;
    esac
    [[ " $seen " != *" $key "* ]] || return 1
    seen+=" $key"
    ((count+=1))
    declare -gx "$key=$value"
  done < "$file"
  [[ $count == 10 && -n ${OAOS_COMPANY_USER:-} && -n ${OAOS_COMPANY_E_ID:-} && -n ${PGDATABASE:-} && -n ${OAOS_COMPANY_NGINX_VHOST:-} ]]
}
company_c02_unit_ready() {
  [[ $(systemctl --user is-enabled "$1" 2>/dev/null || true) == enabled && $(systemctl --user is-active "$1" 2>/dev/null || true) == active ]]
}
company_c02_backup_job() {
  systemctl --user start oaos-company-backup.service >/dev/null 2>&1 || return 1
  [[ $(systemctl --user show oaos-company-backup.service -p Result --value 2>/dev/null) == success ]] || return 1
  [[ -s ${OAOS_COMPANY_BACKUP_DIR:-/nonexistent}/last-success ]]
}
company_c02_linger() { [[ $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null) == yes ]]; }
company_c02_env_mode() {
  [[ $(stat -c %a "$HOME/.config/oaos-company") == 700 && $(stat -c %a "$HOME/.config/oaos-company/.env") == 600 ]]
}
company_c02_loopback() {
  local sockets
  sockets=$(ss -ltn 2>/dev/null) || return 1
  grep -Eq '(^|[[:space:]])127\.0\.0\.1:8765[[:space:]]' <<< "$sockets" || return 1
  ! grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\*|\[::\]|::):8765[[:space:]]' <<< "$sockets"
}
company_c02_private_ports() {
  local sockets
  sockets=$(ss -ltn 2>/dev/null) || return 1
  ! awk '$1 == "LISTEN" && $4 ~ /:(5432|6379|8765)$/ && $4 !~ /^(127\.0\.0\.1|\[::1\]|::1):/ {bad=1} END {exit !bad}' <<< "$sockets"
}
company_c02_https() {
  [[ ${OAOS_COMPANY_HTTPS_ORIGIN:-} =~ ^https://[A-Za-z0-9.-]+$ ]] || return 1
  [[ $(curl --silent --show-error --noproxy '*' --max-time 8 --output /dev/null --write-out '%{http_code}' "$OAOS_COMPANY_HTTPS_ORIGIN/company/health" 2>/dev/null) == 200 ]]
}
company_c02_rebooted() {
  local before now
  [[ -r $HOME/.config/oaos-company/initial-boot-id ]] || return 1
  before=$(< "$HOME/.config/oaos-company/initial-boot-id")
  now=$(< /proc/sys/kernel/random/boot_id)
  [[ -n $before && -n $now && $before != "$now" ]]
}
company_c02_idempotent() {
  local before after
  before=$(sha256sum "$OAOS_COMPANY_NGINX_VHOST" "$HOME/.config/oaos-company/.env" "$HOME/.config/oaos-company/deployment.conf" "$HOME/.config/systemd/user/oaos-company.service" "$HOME/.config/systemd/user/oaos-company-backup.service" "$HOME/.config/systemd/user/oaos-company-backup.timer" 2>/dev/null) || return 1
  bash "$(dirname "${BASH_SOURCE[0]}")/install.sh" --stage c02 >/dev/null 2>&1 || return 1
  after=$(sha256sum "$OAOS_COMPANY_NGINX_VHOST" "$HOME/.config/oaos-company/.env" "$HOME/.config/oaos-company/deployment.conf" "$HOME/.config/systemd/user/oaos-company.service" "$HOME/.config/systemd/user/oaos-company-backup.service" "$HOME/.config/systemd/user/oaos-company-backup.timer" 2>/dev/null) || return 1
  [[ $before == "$after" ]]
}
company_c02_verify() {
  local pass=0 fail=0 cert host
  [[ $(stage_status c02) == applied || $(stage_status c02) == verified ]] || { warn 'C02 has not been applied.'; return 3; }
  company_c02_load_settings || { warn 'C02 deployment settings are missing or invalid.'; return 3; }
  if ! { have_cmd systemctl && have_cmd loginctl && have_cmd ss && have_cmd curl && have_cmd sha256sum; }; then warn 'C02 read-back commands unavailable.'; return 3; fi
  printf '%-27s %s\n' CHECK RESULT
  company_c02_check 'Service enabled/active' company_c02_unit_ready oaos-company.service
  company_c02_check 'Backup timer enabled/active' company_c02_unit_ready oaos-company-backup.timer
  company_c02_check 'Backup service run' company_c02_backup_job
  company_c02_check 'Linger enabled' company_c02_linger
  company_c02_check 'Loopback port 8765' company_c02_loopback
  company_c02_check 'DB/Redis/Company private' company_c02_private_ports
  company_c02_check 'HTTPS Company health' company_c02_https
  company_c02_check 'Environment permissions' company_c02_env_mode
  host=${OAOS_COMPANY_HTTPS_ORIGIN#https://}
  cert="/etc/letsencrypt/live/$host/fullchain.pem"
  company_c02_check 'TLS expiry (30 days)' run_root openssl x509 -checkend 2592000 -noout -in "$cert"
  if { [[ $(systemctl is-enabled certbot.timer 2>/dev/null || true) == enabled && $(systemctl is-active certbot.timer 2>/dev/null || true) == active ]] || [[ $(systemctl is-enabled snap.certbot.renew.timer 2>/dev/null || true) == enabled && $(systemctl is-active snap.certbot.renew.timer 2>/dev/null || true) == active ]]; }; then
    printf '%-27s PASS\n' 'TLS renewal timer'; pass=$((pass + 1))
  else
    printf '%-27s FAIL\n' 'TLS renewal timer'; fail=$((fail + 1))
  fi
  company_c02_check 'Installer rerun stable' company_c02_idempotent
  if ((fail)); then printf 'Summary: PASS=%s FAIL=%s MANUAL=1 SKIP=0\n' "$pass" "$fail"; return 1; fi
  if ! company_c02_rebooted; then
    printf '%-27s MANUAL\n' 'Safe reboot and recovery'
    printf 'Summary: PASS=%s FAIL=0 MANUAL=1 SKIP=0\n' "$pass"
    return 3
  fi
  company_c02_check 'Post-reboot service' company_c02_unit_ready oaos-company.service
  company_c02_check 'Post-reboot HTTPS' company_c02_https
  if ((fail)); then printf 'Summary: PASS=%s FAIL=%s MANUAL=0 SKIP=0\n' "$pass" "$fail"; return 1; fi
  printf 'Summary: PASS=%s FAIL=0 MANUAL=0 SKIP=0\n' "$pass"
  stage_mark c02 verified 'E C02 read-back passed after reboot; installer rerun stable'
}
