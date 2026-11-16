#!/usr/bin/env bash
# Sourced by install.sh. C05 uses the C02 E host and its dedicated account.
repo_root=${repo_root:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}
# shellcheck source=editions/company/lib/stage.sh
. "$repo_root/editions/company/lib/stage.sh"

company_c05_apply() {
  local env_file key_snapshot
  company_stage_ready c05 c01 c02 c03 c04 || return $?
  python3 -c 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM' 2>/dev/null || {
    warn 'C05 BLOCKED: Python cryptography AES-GCM is unavailable.'; return 3;
  }
  env_file="$HOME/.config/oaos-company/.env"
  [[ -f $env_file ]] || { warn 'C05 BLOCKED: Company EnvironmentFile is missing.'; return 3; }
  company_stage_migrate c05 4 || return $?
  if ! ((COMPANY_STAGE_MIGRATED)); then
    python3 "$repo_root/editions/company/app/vault.py" check-key "$env_file" || return 3
    info 'C05 vault key confirmed.'
    return 0
  fi
  python3 "$repo_root/editions/company/app/vault.py" ensure-key "$env_file" || return 3
  key_snapshot=$(mktemp -d "$OAOS_COMPANY_KEY_BACKUP_DIR/c05-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX") || return 1
  chmod 700 "$key_snapshot"
  (umask 077; cp -- "$env_file" "$key_snapshot/company.env") || return 1
  chmod 600 "$key_snapshot/company.env"
  info 'C05 migration applied; run E personal credential read-back.'
}
