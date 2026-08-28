#!/usr/bin/env bash
# restore.sh — Business edition restore (decrypt + pg_restore + redis)
# Usage: ./deploy/scripts/restore.sh --manifest <manifest.json> [--postgres-only|--redis-only] [--dry-run]
#    or: ./deploy/scripts/restore.sh --pg-file <file> --redis-file <file> [--dry-run]
# Env: POSTGRES_*, DATABASE_URL, REDIS_*, AGE_PRIVATE_KEY / AGE_IDENTITY, GPG_*, BACKUP_DIR
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DRY_RUN=0
MANIFEST=""
PG_FILE=""
REDIS_FILE=""
MODE="all"  # all | postgres-only | redis-only

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --manifest) MANIFEST="$2"; shift 2 ;;
    --pg-file) PG_FILE="$2"; shift 2 ;;
    --redis-file) REDIS_FILE="$2"; shift 2 ;;
    --postgres-only) MODE="postgres-only"; shift ;;
    --redis-only) MODE="redis-only"; shift ;;
    --help|-h)
      echo "Usage: $0 --manifest <manifest.json> [--postgres-only|--redis-only] [--dry-run]"
      echo "   or: $0 --pg-file <file> --redis-file <file> [--dry-run]"
      exit 0
      ;;
    *) echo "[WARN] Unknown arg: $1" >&2; shift ;;
  esac
done

if [[ "${OAOS_RESTORE_DRY_RUN:-}" == "1" ]]; then DRY_RUN=1; fi

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2; }
dry_prefix() { if [[ $DRY_RUN -eq 1 ]]; then echo "[DRY-RUN] "; fi; }

decrypt_file() {
  local src="$1" dst=""
  if [[ "${src}" == *.age ]]; then
    dst="${src%.age}"
    if [[ $DRY_RUN -eq 1 ]]; then
      log "$(dry_prefix)Would decrypt age ${src} -> ${dst}"
      echo "${src}"
      return 0
    fi
    if ! command -v age >/dev/null 2>&1; then
      log "[ERROR] age not found — cannot decrypt ${src}"; return 1; fi
    local key="${AGE_PRIVATE_KEY:-${AGE_IDENTITY:-}}"
    if [[ -z "${key}" ]] && [[ -n "${AGE_IDENTITY_FILE:-}" ]] && [[ -f "${AGE_IDENTITY_FILE}" ]]; then
      age --decrypt --identity "${AGE_IDENTITY_FILE}" --output "${dst}" "${src}" || return 1
    elif [[ -n "${key}" ]]; then
      local tmpkey
      tmpkey=$(mktemp)
      echo "${key}" > "${tmpkey}"; chmod 600 "${tmpkey}"
      age --decrypt --identity "${tmpkey}" --output "${dst}" "${src}" || { rm -f "${tmpkey}"; return 1; }
      rm -f "${tmpkey}"
    else
      log "[ERROR] AGE_PRIVATE_KEY/AGE_IDENTITY not set for age decrypt"; return 1
    fi
    log "[OK] Decrypted ${src} -> ${dst}"
    echo "${dst}"
  elif [[ "${src}" == *.gpg ]]; then
    dst="${src%.gpg}"
    if [[ $DRY_RUN -eq 1 ]]; then
      log "$(dry_prefix)Would decrypt gpg ${src} -> ${dst}"
      echo "${src}"
      return 0
    fi
    if ! command -v gpg >/dev/null 2>&1; then
      log "[ERROR] gpg not found — cannot decrypt ${src}"; return 1; fi
    gpg --decrypt --output "${dst}" "${src}" || return 1
    log "[OK] Decrypted ${src} -> ${dst}"
    echo "${dst}"
  else
    echo "${src}"
  fi
}

resolve_from_manifest() {
  if [[ -n "${MANIFEST}" ]] && [[ -f "${MANIFEST}" ]]; then
    local dir base_pg base_redis
    dir=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('backup_dir',''))" "${MANIFEST}" 2>/dev/null || echo "")
    base_pg=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('postgres_file',''))" "${MANIFEST}" 2>/dev/null || echo "")
    base_redis=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('redis_file',''))" "${MANIFEST}" 2>/dev/null || echo "")
    if [[ -z "${dir}" ]]; then dir="$(dirname "${MANIFEST}")"; fi
    if [[ -z "${PG_FILE}" ]] && [[ -n "${base_pg}" ]]; then PG_FILE="${dir}/${base_pg}"; fi
    if [[ -z "${REDIS_FILE}" ]] && [[ -n "${base_redis}" ]]; then REDIS_FILE="${dir}/${base_redis}"; fi
    log "[INFO] Resolved from manifest: pg=${PG_FILE} redis=${REDIS_FILE}"
  fi
}

restore_postgres() {
  local src="$1"
  if [[ -z "${src}" ]] || [[ ! -f "${src}" ]]; then
    log "[WARN] Postgres file not found: ${src} — skipping"; return 0; fi
  log "$(dry_prefix)Restoring Postgres from ${src}"
  local plain
  plain=$(decrypt_file "${src}") || { log "[ERROR] Decrypt failed for ${src}"; return 1; }
  # plain may be same as src in dry-run (still encrypted path); handle gzip
  local sql_file="${plain}"
  # If it's still .age/.gpg path in dry-run, skip actual restore
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would run psql < ${sql_file} (or gunzip | psql)"
    return 0
  fi
  # Decompress if .gz
  local decompressed=""
  if [[ "${plain}" == *.gz ]]; then
    decompressed="${plain%.gz}"
    gunzip -c "${plain}" > "${decompressed}" || { log "[ERROR] gunzip failed"; return 1; }
    sql_file="${decompressed}"
  fi
  # Try docker exec first
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-postgres"; then
    local pg_user="${POSTGRES_USER:-open_agent_os}"
    local pg_db="${POSTGRES_DB:-open_agent_os}"
    log "[INFO] Restoring via docker exec oaos-postgres psql"
    docker exec -i oaos-postgres psql -U "${pg_user}" -d "${pg_db}" < "${sql_file}" || {
      log "[ERROR] docker psql restore failed"; return 1; }
  elif command -v psql >/dev/null 2>&1; then
    local pg_url="${DATABASE_URL:-}"
    if [[ -n "${pg_url}" ]]; then
      pg_url="${pg_url/postgresql+asyncpg:/postgresql:}"
      psql "${pg_url}" < "${sql_file}" || { log "[ERROR] psql restore failed"; return 1; }
    else
      local pg_user="${POSTGRES_USER:-open_agent_os}"
      local pg_db="${POSTGRES_DB:-open_agent_os}"
      local pg_host="${POSTGRES_HOST:-localhost}"
      local pg_port="${POSTGRES_PORT:-5432}"
      PGPASSWORD="${POSTGRES_PASSWORD:-secret}" psql -h "${pg_host}" -p "${pg_port}" -U "${pg_user}" -d "${pg_db}" < "${sql_file}" || {
        log "[ERROR] psql restore failed"; return 1; }
    fi
  else
    log "[ERROR] No docker nor psql available for restore"; return 1
  fi
  log "[OK] Postgres restore complete from ${src}"
  # Cleanup temp decrypted file if we created it
  if [[ "${plain}" != "${src}" ]] && [[ -f "${plain}" ]]; then
    # keep if it was original unencrypted file; remove only if we decrypted
    if [[ "${src}" == *.age ]] || [[ "${src}" == *.gpg ]]; then
      rm -f "${plain}" "${decompressed:-}" || true
    else
      rm -f "${decompressed:-}" || true
    fi
  else
    rm -f "${decompressed:-}" || true
  fi
}

restore_redis() {
  local src="$1"
  if [[ -z "${src}" ]] || [[ ! -f "${src}" ]]; then
    log "[WARN] Redis file not found: ${src} — skipping"; return 0; fi
  log "$(dry_prefix)Restoring Redis from ${src}"
  local plain
  plain=$(decrypt_file "${src}") || { log "[ERROR] Decrypt failed for ${src}"; return 1; }
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would restore Redis RDB ${plain} (copy to /data/dump.rdb + restart or DEBUG RELOAD)"
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-redis"; then
    log "[INFO] Restoring Redis via docker cp + restart"
    # Copy RDB into container and restart redis to load it (or use DEBUG RELOAD)
    docker cp "${plain}" oaos-redis:/data/dump.rdb || { log "[ERROR] docker cp failed"; return 1; }
    # Try DEBUG RELOAD first (no downtime), fallback to restart
    local redis_pw="${REDIS_PASSWORD:-}"
    local reload_ok=0
    if [[ -n "${redis_pw}" ]]; then
      docker exec oaos-redis redis-cli -a "${redis_pw}" DEBUG RELOAD 2>/dev/null && reload_ok=1 || true
    else
      docker exec oaos-redis redis-cli DEBUG RELOAD 2>/dev/null && reload_ok=1 || true
    fi
    if [[ $reload_ok -eq 0 ]]; then
      log "[INFO] DEBUG RELOAD not available — restarting oaos-redis"
      docker restart oaos-redis >/dev/null
      sleep 3
    fi
  elif command -v redis-cli >/dev/null 2>&1; then
    log "[WARN] Host redis-cli restore: copying RDB to ${REDIS_RDB_DEST:-/tmp/oaos-restore-dump.rdb} — manual restart may be needed"
    cp "${plain}" "${REDIS_RDB_DEST:-/tmp/oaos-restore-dump.rdb}" || return 1
    # Attempt DEBUG RELOAD if connected
    local redis_pw="${REDIS_PASSWORD:-}"
    if [[ -n "${redis_pw}" ]]; then
      redis-cli -a "${redis_pw}" DEBUG RELOAD 2>/dev/null || log "[WARN] redis DEBUG RELOAD failed — restart redis-server manually"
    else
      redis-cli DEBUG RELOAD 2>/dev/null || log "[WARN] redis DEBUG RELOAD failed — restart redis-server manually"
    fi
  else
    log "[ERROR] No docker nor redis-cli available for restore"; return 1
  fi
  log "[OK] Redis restore complete from ${src}"
  if [[ "${plain}" != "${src}" ]] && [[ -f "${plain}" ]] && { [[ "${src}" == *.age ]] || [[ "${src}" == *.gpg ]]; }; then
    rm -f "${plain}" || true
  fi
}

main() {
  log "=== OAOS Restore start dry_run=${DRY_RUN} mode=${MODE} ==="
  if [[ -n "${MANIFEST}" ]]; then
    resolve_from_manifest
  fi
  if [[ -z "${PG_FILE}" ]] && [[ -z "${REDIS_FILE}" ]] && [[ -z "${MANIFEST}" ]]; then
    echo "Usage: $0 --manifest <manifest.json> [--dry-run]" >&2
    echo "   or: $0 --pg-file <file> --redis-file <file> [--dry-run]" >&2
    exit 2
  fi
  local fail=0
  if [[ "${MODE}" == "all" ]] || [[ "${MODE}" == "postgres-only" ]]; then
    restore_postgres "${PG_FILE}" || fail=1
  fi
  if [[ "${MODE}" == "all" ]] || [[ "${MODE}" == "redis-only" ]]; then
    restore_redis "${REDIS_FILE}" || fail=1
  fi
  if [[ $fail -ne 0 ]]; then
    log "[ERROR] Restore completed with errors"
    exit 1
  fi
  log "=== OAOS Restore complete (dry_run=${DRY_RUN}) ==="
}

main "$@"
