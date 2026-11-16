#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
# Sourced by install.sh. C17 collects final handover evidence on the E host:
# migration 012, a verified backup, a recovery drill, reboot survival, systemd
# state and the external round-trip for every selected integration.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/stages/c16.sh
. "$repo_root/editions/company/stages/c16.sh"

company_c17_block() { warn "C17 BLOCKED: $1"; return 3; }

company_c17_providers() {
  company_sql_value "$1" "$2" \
    "SELECT string_agg(provider, ',' ORDER BY provider)
       FROM company.integration_settings
      WHERE org_id = '$1'::uuid AND state = 'selected'"
}

company_c17_roundtrip_json() {
  local selected=$1 confirmed=$2
  OAOS_C17_SELECTED="$selected" OAOS_C17_CONFIRMED="$confirmed" python3 - <<'PY'
import json, os
selected = [p for p in os.environ.get('OAOS_C17_SELECTED', '').split(',') if p]
confirmed = {p for p in os.environ.get('OAOS_C17_CONFIRMED', '').split(',') if p}
print(json.dumps({p: ('confirmed' if p in confirmed else 'pending')
                  for p in selected}, sort_keys=True))
PY
}

company_c17_check_roundtrip() {
  local selected=$1 confirmed=${OAOS_COMPANY_C17_ROUNDTRIP:-} provider entry
  local -a entries=()
  local valid='slack notion google_workspace microsoft_365'
  IFS=',' read -r -a entries <<< "$confirmed"
  for entry in "${entries[@]}"; do
    [[ -z $entry ]] && continue
    [[ " $valid " == *" $entry "* ]] || {
      company_c17_block "unknown round-trip provider: $entry"; return 3
    }
  done
  IFS=',' read -r -a entries <<< "$selected"
  for provider in "${entries[@]}"; do
    [[ -z $provider ]] && continue
    [[ ",$confirmed," == *",$provider,"* ]] || {
      company_c17_block "external round-trip evidence is missing for $provider"
      return 3
    }
  done
}

company_c17_latest_backup() {
  company_sql_value "$1" "$2" \
    "SELECT id::text || '|' || location || '|' || checksum || '|' || size_bytes
       FROM company.backup_runs
      WHERE org_id = '$1'::uuid AND state = 'verified'
      ORDER BY created_at DESC, id DESC LIMIT 1"
}

company_c17_latest_recovery() {
  company_sql_value "$1" "$2" \
    "SELECT id::text
       FROM company.recovery_drills
      WHERE org_id = '$1'::uuid AND backup_id = '$3'::uuid AND state = 'passed'
      ORDER BY completed_at DESC, id DESC LIMIT 1"
}

company_c17_file_ok() {
  local location=$1 checksum=$2 size=$3 actual
  [[ -f $location && -r $location ]] || return 1
  actual=$(sha256sum "$location" 2>/dev/null) || return 1
  [[ ${actual%% *} == "$checksum" ]] || return 1
  [[ $(stat -c %s "$location" 2>/dev/null) == "$size" ]]
}

company_c17_write_backup() {
  local target=$1 tmp="$1.tmp.$$"
  company_maint_socket_ok || return 3
  company_maint_dump "$tmp" || { rm -f -- "$tmp"; return 1; }
  mv -- "$tmp" "$target" || { rm -f -- "$tmp"; return 1; }
}

company_c17_restore_drill() {
  local backup=$1 scratch=$2 socket_dir=${OAOS_COMPANY_PG_SOCKET_DIR:-/var/run/postgresql}
  local owner version live rc=0
  [[ $scratch =~ ^oaos_company_c17_restore_[0-9]{14}_[0-9]+$ ]] || return 1
  owner=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT current_user' 2>/dev/null) || return 1
  [[ $owner =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
  company_maint createdb --host="$socket_dir" --port="${PGPORT:-5432}" \
    --owner="$owner" "$scratch" >/dev/null 2>&1 || return 1
  if ! pg_restore --no-owner --no-privileges --dbname="$scratch" "$backup" \
      >/dev/null 2>&1; then
    rc=1
  fi
  if ((rc == 0)); then
    # The restored history must equal the live history (base 012 plus amendments).
    live=$(psql -X -q -A -t -v ON_ERROR_STOP=1 \
      -c "SELECT string_agg(version::text || ':' || checksum, ',' ORDER BY version) FROM company.schema_migrations" 2>/dev/null) || rc=1
    version=$(psql -X -q -A -t -v ON_ERROR_STOP=1 --dbname="$scratch" \
      -c "SELECT string_agg(version::text || ':' || checksum, ',' ORDER BY version) FROM company.schema_migrations" 2>/dev/null) || rc=1
    [[ -n $live && $version == "$live" && ,$live, == *,12:* ]] || rc=1
  fi
  company_maint dropdb --host="$socket_dir" --port="${PGPORT:-5432}" \
    --if-exists "$scratch" >/dev/null 2>&1 || true
  return "$rc"
}

# Daily retention (migration 018): a user oneshot unit + timer running
# maintenance.sh through the postgres maintenance path. Like the backup unit it
# omits NoNewPrivileges because that path needs the C02 non-interactive root.
company_c17_install_maintenance() {
  local unit_dir="$HOME/.config/systemd/user" app_dir="$HOME/.local/lib/oaos-company"
  local config_dir="$HOME/.config/oaos-company" unit
  install -d -m 700 "$app_dir" "$unit_dir" || return 1
  install -m 700 "$repo_root/editions/company/app/maintenance.sh" "$app_dir/maintenance.sh" || return 1
  cat > "$unit_dir/oaos-company-maintenance.service" <<EOF
[Unit]
Description=OAOS Company retention (180-day audit, 30-day departed personal content)

[Service]
Type=oneshot
EnvironmentFile=$config_dir/.env
ExecStart=$app_dir/maintenance.sh
UMask=0077
EOF
  cat > "$unit_dir/oaos-company-maintenance.timer" <<'EOF'
[Unit]
Description=OAOS Company daily retention timer

[Timer]
OnCalendar=*-*-* 04:30:00
Persistent=true
Unit=oaos-company-maintenance.service

[Install]
WantedBy=timers.target
EOF
  chmod 600 "$unit_dir/oaos-company-maintenance.service" "$unit_dir/oaos-company-maintenance.timer"
  systemctl --user daemon-reload || return 1
  systemctl --user enable --now oaos-company-maintenance.timer >/dev/null || return 1
  unit=oaos-company-maintenance.timer
  [[ $(systemctl --user is-enabled "$unit" 2>/dev/null) == enabled &&
     $(systemctl --user is-active "$unit" 2>/dev/null) == active ]] || return 1
  # Run once now so the handover carries retention evidence.
  systemctl --user start oaos-company-maintenance.service >/dev/null 2>&1
}

company_c17_apply() {
  local org='' admin='' selected='' confirmed='' roundtrip='' evidence=''
  local boot_id='' previous_boot_id='' reboot_verified=false service_state=''
  local stamp target location checksum size
  local latest backup_id recovery_id drill_scratch record evidence_json
  local decision

  [[ $(stage_status c01) == verified && $(stage_status c02) == verified && \
     $(stage_status c10) == verified && $(stage_status c15) == verified && \
     ( $(stage_status c16) == verified || $(stage_status c16) == SKIP ) ]] || {
    company_c17_block 'C01, C02, C10 and C15 must be verified and C16 verified or SKIP on E.'; return 3;
  }
  company_stage_ready c17 || return $?
  if ! have_cmd pg_restore || ! have_cmd createdb || ! have_cmd dropdb; then
    company_c17_block 'PostgreSQL backup/restore tools and Python are required.'; return 3
  fi

  read -r org admin < <(company_admin_context) || {
    company_c17_block 'a single organization with an active G10 administrator is required.'; return 3;
  }
  [[ -n $org && -n $admin ]] || {
    company_c17_block 'a single organization with an active G10 administrator is required.'; return 3;
  }

  company_stage_migrate c17 12 || return $?

  company_c17_install_maintenance || {
    warn 'C17 retention timer could not be installed or its first run failed.'; return 1;
  }

  selected=$(company_c17_providers "$org" "$admin") || return 1
  confirmed=${OAOS_COMPANY_C17_ROUNDTRIP:-}
  company_c17_check_roundtrip "$selected" || return $?
  roundtrip=$(company_c17_roundtrip_json "$selected" "$confirmed") || return 1

  service_state=''
  if [[ $(systemctl --user is-enabled oaos-company.service 2>/dev/null || true) == enabled &&
        $(systemctl --user is-active oaos-company.service 2>/dev/null || true) == active &&
        $(systemctl --user is-enabled oaos-company-backup.timer 2>/dev/null || true) == enabled &&
        $(systemctl --user is-active oaos-company-backup.timer 2>/dev/null || true) == active ]]; then
    service_state='active'
  else
    company_c17_block 'Company service and backup timer must be enabled and active.'; return 3
  fi

  boot_id=$(tr -d '\r\n' < /proc/sys/kernel/random/boot_id 2>/dev/null) || boot_id=''
  if [[ -r $HOME/.config/oaos-company/initial-boot-id ]]; then
    previous_boot_id=$(tr -d '\r\n' < "$HOME/.config/oaos-company/initial-boot-id")
  fi
  if [[ -n $boot_id && -n $previous_boot_id && $boot_id != "$previous_boot_id" ]]; then
    reboot_verified=true
  fi

  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  latest=$(company_c17_latest_backup "$org" "$admin") || latest=''
  backup_id=''
  if [[ -n $latest ]]; then
    IFS='|' read -r backup_id location checksum size <<< "$latest"
    if ! company_c17_file_ok "$location" "$checksum" "$size"; then
      backup_id=''
    fi
  fi

  if [[ -z $backup_id ]]; then
    target="$OAOS_COMPANY_BACKUP_DIR/company-c17-$stamp.dump"
    company_c17_write_backup "$target" || {
      company_c17_block 'E database backup failed or is empty.'; return 3
    }
    location=$target
    checksum=$(sha256sum "$location")
    checksum=${checksum%% *}
    size=$(stat -c %s "$location")
    pg_restore --list "$location" >/dev/null 2>&1 || {
      warn 'C17 backup archive read-back failed.'; return 1
    }
    record=$(company_sql_value "$org" "$admin" \
      "SELECT decision || '|' || backup_id FROM company.c17_record_backup('pre_handover', '$location', '$checksum', $size)") || return 1
    [[ $record == recorded\|* ]] || { warn 'C17 BLOCKED: backup evidence was rejected.'; return 1; }
    backup_id=${record#*|}
  else
    pg_restore --list "$location" >/dev/null 2>&1 || {
      warn 'C17 backup archive read-back failed.'; return 1
    }
    info 'C17 reused the existing verified handover backup.'
  fi

  recovery_id=$(company_c17_latest_recovery "$org" "$admin" "$backup_id") || recovery_id=''
  if [[ -z $recovery_id ]]; then
    drill_scratch="oaos_company_c17_restore_$(date -u +%Y%m%d%H%M%S)_$$"
    company_c17_restore_drill "$location" "$drill_scratch" || {
      warn 'C17 recovery drill failed; the backup was not verified.'; return 1
    }
    evidence_json="{\"schema_version\":12,\"target\":\"$drill_scratch\"}"
    record=$(company_sql_value "$org" "$admin" \
      "SELECT decision || '|' || recovery_id FROM company.c17_record_recovery('$backup_id'::uuid, '$drill_scratch', true, '$evidence_json'::jsonb)") || return 1
    [[ $record == recorded\|* ]] || { warn 'C17 BLOCKED: recovery evidence was rejected.'; return 1; }
    recovery_id=${record#*|}
  else
    info 'C17 reused the existing passing recovery drill.'
  fi

  evidence="{\"service\":\"$service_state\",\"boot_id\":\"$boot_id\",\"previous_boot_id\":\"$previous_boot_id\",\"backup\":\"$location\",\"checksum\":\"$checksum\",\"recovery_id\":\"$recovery_id\"}"
  record=$(company_sql_value "$org" "$admin" \
    "SELECT decision FROM company.c17_record_handover('$service_state', '$boot_id', '$previous_boot_id', $reboot_verified, '$roundtrip'::jsonb, '$evidence'::jsonb)") || return 1
  decision=${record%%$'\n'*}
  case $decision in
    verified) info "C17 handover verified: backup, recovery, systemd and ${selected:-no selected integrations}." ;;
    pending)
      if [[ $reboot_verified != true ]]; then
        info 'C17 backup/recovery applied; reboot survival is still manual.'
      else
        info 'C17 backup/recovery applied; handover evidence is still pending.'
      fi ;;
    *) warn 'C17 BLOCKED: handover evidence was rejected.'; return 1 ;;
  esac
  info 'C17 final validation applied; run the E read-back.'
}
