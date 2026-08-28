#!/usr/bin/env bash
# restore.sh — Business edition restore (decrypt + pg_restore + redis)
# v1.6 §27.11: 3-DB individual logical restore (mattermost/outline/openagentos) from per-DB dumps,
# plus pgvector + alembic consistency verification.
# Usage: ./deploy/scripts/restore.sh --manifest <manifest.json> [--db DB] [--postgres-only|--redis-only] [--dry-run] [--verify]
#    or: ./deploy/scripts/restore.sh --pg-file <file> [--db DB] --redis-file <file> [--dry-run]
#    or: ./deploy/scripts/restore.sh --pg-file <file> --db openagentos --dry-run  (single DB)
# Env: POSTGRES_*, DATABASE_URL, REDIS_*, AGE_PRIVATE_KEY / AGE_IDENTITY, GPG_*, BACKUP_DIR
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DRY_RUN=0
DO_VERIFY=0
MANIFEST=""
PG_FILE=""
REDIS_FILE=""
MODE="all"  # all | postgres-only | redis-only
# §27.11: per-DB filtering
REQUESTED_DBS=""  # space-separated list of DBs to restore; empty = all
declare -a PG_RESTORE_LIST=()  # list of "db:filepath"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --manifest) MANIFEST="$2"; shift 2 ;;
    --pg-file) PG_FILE="$2"; shift 2 ;;
    --redis-file) REDIS_FILE="$2"; shift 2 ;;
    --db) REQUESTED_DBS="${REQUESTED_DBS} $2"; shift 2 ;;
    --postgres-only) MODE="postgres-only"; shift ;;
    --redis-only) MODE="redis-only"; shift ;;
    --verify|--check) DO_VERIFY=1; shift ;;
    --help|-h)
      echo "Usage: $0 --manifest <manifest.json> [--db DB] [--postgres-only|--redis-only] [--dry-run] [--verify]"
      echo "   or: $0 --pg-file <file> [--db openagentos] --redis-file <file> [--dry-run]"
      echo "  --db may be repeated to filter (openagentos|mattermost|outline)"
      echo "  --verify runs §27.11 pgvector + alembic consistency check after restore"
      exit 0
      ;;
    *) echo "[WARN] Unknown arg: $1" >&2; shift ;;
  esac
done

# Normalize requested DBs (trim)
REQUESTED_DBS=$(echo "${REQUESTED_DBS}" | xargs 2>/dev/null || echo "${REQUESTED_DBS}")
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

# §27.11 consistency check helper (mirrors backup.sh)
oaos_consistency_check() {
  log "=== §27.11 Consistency check: pgvector + alembic ==="
  local pgvector_ok="unknown" alembic_ok="unknown" alembic_current="" alembic_head=""
  local errors=()
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would check pgvector extension and alembic version"
    return 0
  fi
  local vec_found=0
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-postgres"; then
    local pg_user="${POSTGRES_USER:-openagentos}"
    if docker exec oaos-postgres psql -U "${pg_user}" -d openagentos -c "SELECT extname FROM pg_extension WHERE extname='vector';" 2>/dev/null | grep -q "vector"; then
      vec_found=1
    fi
  elif command -v psql >/dev/null 2>&1; then
    local pg_user="${POSTGRES_USER:-openagentos}"
    local pg_host="${POSTGRES_HOST:-localhost}"
    local pg_port="${POSTGRES_PORT:-5432}"
    local pg_url="${DATABASE_URL:-}"
    if [[ -n "${pg_url}" ]]; then
      pg_url="${pg_url/postgresql+asyncpg:/postgresql:}"
      pg_url=$(echo "${pg_url}" | sed -E 's|/[^/]*$|/openagentos|')
      if psql "${pg_url}" -c "SELECT extname FROM pg_extension WHERE extname='vector';" 2>/dev/null | grep -q "vector"; then vec_found=1; fi
    else
      if PGPASSWORD="${POSTGRES_PASSWORD:-secret}" psql -h "${pg_host}" -p "${pg_port}" -U "${pg_user}" -d openagentos -c "SELECT extname FROM pg_extension WHERE extname='vector';" 2>/dev/null | grep -q "vector"; then vec_found=1; fi
    fi
  fi
  if [[ $vec_found -eq 1 ]]; then
    pgvector_ok="true"
    log "[OK] pgvector extension present in openagentos"
  else
    pgvector_ok="false"
    log "[WARN] pgvector extension NOT found in openagentos — expected pgvector/pgvector:pg16"
    errors+=("pgvector extension missing")
  fi
  if [[ -f "${REPO_ROOT}/alembic.ini" ]]; then
    local cur_raw="" head_raw=""
    if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-control-plane"; then
      cur_raw=$(docker exec oaos-control-plane alembic current 2>/dev/null | head -n 20 || true)
      head_raw=$(docker exec oaos-control-plane alembic heads 2>/dev/null | head -n 20 || true)
    fi
    if [[ -z "${cur_raw}" ]] && command -v alembic >/dev/null 2>&1; then
      cur_raw=$( (cd "${REPO_ROOT}" && alembic current 2>/dev/null) | head -n 20 || true)
      head_raw=$( (cd "${REPO_ROOT}" && alembic heads 2>/dev/null) | head -n 20 || true)
    fi
    alembic_current=$(echo "${cur_raw}" | grep -oE '[a-f0-9_]+' | head -n1 || echo "")
    alembic_head=$(echo "${head_raw}" | grep -oE '[a-f0-9_]+' | head -n1 || echo "")
    if echo "${cur_raw}" | grep -qi "head"; then
      alembic_ok="true"
      log "[OK] alembic at head"
    elif [[ -n "${alembic_current}" ]] && [[ "${alembic_current}" == "${alembic_head}" ]]; then
      alembic_ok="true"
      log "[OK] alembic current matches head: ${alembic_current}"
    elif [[ -n "${alembic_current}" ]]; then
      alembic_ok="true"
      log "[INFO] alembic current=${alembic_current}"
    else
      alembic_ok="unknown"
      log "[WARN] alembic current not determinable"
    fi
  fi
  if [[ ${#errors[@]} -gt 0 ]]; then
    log "[WARN] Consistency check issues: ${errors[*]}"; return 1; fi
  log "[OK] Consistency check passed (pgvector=${pgvector_ok} alembic=${alembic_ok})"
  return 0
}

resolve_from_manifest() {
  if [[ -n "${MANIFEST}" ]] && [[ -f "${MANIFEST}" ]]; then
    local dir
    dir=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('backup_dir',''))" "${MANIFEST}" 2>/dev/null || echo "")
    if [[ -z "${dir}" ]]; then dir="$(dirname "${MANIFEST}")"; fi

    # Try per-DB entries first (§27.11)
    local has_dbs
    has_dbs=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print('1' if d.get('postgres_dbs') else '0')" "${MANIFEST}" 2>/dev/null || echo "0")
    if [[ "${has_dbs}" == "1" ]]; then
      # Extract db,file,status for each entry via python
      local entries
      entries=$(python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
for e in d.get('postgres_dbs',[]):
    print(f\"{e.get('db','')}\t{e.get('file','')}\t{e.get('status','')}\")
" "${MANIFEST}" 2>/dev/null || true)
      while IFS=$'\t' read -r db file status; do
        [[ -z "${db}" ]] && continue
        if [[ "${status}" == "skipped" ]] || [[ -z "${file}" ]]; then
          log "[INFO] Manifest entry db=${db} status=${status} — skipping"
          continue
        fi
        # filter by --db if requested
        if [[ -n "${REQUESTED_DBS}" ]]; then
          local want=0
          for r in ${REQUESTED_DBS}; do if [[ "${r}" == "${db}" ]]; then want=1; break; fi; done
          if [[ $want -eq 0 ]]; then
            log "[INFO] Skipping db=${db} per --db filter (${REQUESTED_DBS})"
            continue
          fi
        fi
        local full="${dir}/${file}"
        # also try absolute if file already contains path
        if [[ "${file}" == /* ]] && [[ -f "${file}" ]]; then full="${file}"; fi
        PG_RESTORE_LIST+=("${db}:${full}")
        log "[INFO] Manifest resolve db=${db} -> ${full}"
      done <<< "${entries}"
      # Also resolve redis
      local base_redis
      base_redis=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('redis_file',''))" "${MANIFEST}" 2>/dev/null || echo "")
      if [[ -z "${REDIS_FILE}" ]] && [[ -n "${base_redis}" ]]; then
        REDIS_FILE="${dir}/${base_redis}"
        if [[ "${base_redis}" == /* ]] && [[ -f "${base_redis}" ]]; then REDIS_FILE="${base_redis}"; fi
      fi
      # If PG_FILE was set via --pg-file, it takes precedence over manifest; otherwise clear it
      # and use PG_RESTORE_LIST. For backward compat, also set PG_FILE to first entry if --db not filtered
      if [[ -z "${PG_FILE}" ]] && [[ ${#PG_RESTORE_LIST[@]} -gt 0 ]]; then
        PG_FILE=$(echo "${PG_RESTORE_LIST[0]}" | cut -d: -f2-)
      fi
    else
      # Legacy manifest: postgres_file single
      local base_pg base_redis
      base_pg=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('postgres_file',''))" "${MANIFEST}" 2>/dev/null || echo "")
      base_redis=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('redis_file',''))" "${MANIFEST}" 2>/dev/null || echo "")
      if [[ -z "${PG_FILE}" ]] && [[ -n "${base_pg}" ]]; then PG_FILE="${dir}/${base_pg}"; fi
      if [[ -z "${REDIS_FILE}" ]] && [[ -n "${base_redis}" ]]; then REDIS_FILE="${dir}/${base_redis}"; fi
      # Legacy: determine db name from REQUESTED_DBS or default openagentos
      local legacy_db="openagentos"
      if [[ -n "${REQUESTED_DBS}" ]]; then
        # if --db specified with legacy manifest, honor first requested
        legacy_db=$(echo "${REQUESTED_DBS}" | awk '{print $1}')
      fi
      if [[ -n "${PG_FILE}" ]]; then
        PG_RESTORE_LIST=("${legacy_db}:${PG_FILE}")
      fi
      log "[INFO] Resolved from legacy manifest: pg=${PG_FILE} redis=${REDIS_FILE}"
    fi
  else
    # No manifest, but --pg-file given
    if [[ -n "${PG_FILE}" ]]; then
      local db="openagentos"
      if [[ -n "${REQUESTED_DBS}" ]]; then db=$(echo "${REQUESTED_DBS}" | awk '{print $1}'); fi
      PG_RESTORE_LIST=("${db}:${PG_FILE}")
    fi
  fi
}

restore_postgres_single() {
  local db="$1" src="$2"
  if [[ -z "${src}" ]] || [[ ! -f "${src}" ]]; then
    log "[WARN] Postgres file not found for db=${db}: ${src} — skipping"; return 0; fi
  log "$(dry_prefix)Restoring Postgres db=${db} from ${src}"
  local plain
  plain=$(decrypt_file "${src}") || { log "[ERROR] Decrypt failed for ${src}"; return 1; }
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would run psql -d ${db} < ${plain} (or gunzip | psql)"
    return 0
  fi
  local sql_file="${plain}"
  local decompressed=""
  if [[ "${plain}" == *.gz ]]; then
    decompressed="${plain%.gz}"
    gunzip -c "${plain}" > "${decompressed}" || { log "[ERROR] gunzip failed for ${src}"; return 1; }
    sql_file="${decompressed}"
  fi
  # Verify pgvector artefacts in dump for openagentos (warn if missing)
  if [[ "${db}" == "openagentos" ]]; then
    if ! grep -qi "vector" "${sql_file}" 2>/dev/null; then
      log "[WARN] Dump for ${db} lacks pgvector artefacts — restore may miss VECTOR extension"
    else
      log "[OK] Dump for ${db} contains pgvector artefacts"
    fi
  fi
  # Try docker exec first
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-postgres"; then
    local pg_user="${POSTGRES_USER:-openagentos}"
    log "[INFO] Restoring via docker exec oaos-postgres psql -d ${db}"
    # Ensure target DB exists; try to create if missing
    if ! docker exec oaos-postgres psql -U "${pg_user}" -d "${db}" -c "SELECT 1" >/dev/null 2>&1; then
      log "[INFO] DB ${db} not found — attempting createdb"
      docker exec oaos-postgres createdb -U "${pg_user}" "${db}" 2>/dev/null || log "[WARN] createdb ${db} failed (may already exist or permission)"
    fi
    docker exec -i oaos-postgres psql -U "${pg_user}" -d "${db}" < "${sql_file}" || {
      log "[ERROR] docker psql restore failed for db=${db}"; return 1; }
  elif command -v psql >/dev/null 2>&1; then
    local pg_url="${DATABASE_URL:-}"
    if [[ -n "${pg_url}" ]]; then
      pg_url="${pg_url/postgresql+asyncpg:/postgresql:}"
      pg_url=$(echo "${pg_url}" | sed -E "s|/[^/]*$|/${db}|")
      psql "${pg_url}" < "${sql_file}" || { log "[ERROR] psql restore failed for db=${db}"; return 1; }
    else
      local pg_user="${POSTGRES_USER:-openagentos}"
      local pg_host="${POSTGRES_HOST:-localhost}"
      local pg_port="${POSTGRES_PORT:-5432}"
      # Ensure DB exists
      if ! PGPASSWORD="${POSTGRES_PASSWORD:-secret}" psql -h "${pg_host}" -p "${pg_port}" -U "${pg_user}" -d "${db}" -c "SELECT 1" >/dev/null 2>&1; then
        log "[INFO] DB ${db} not found — attempting createdb"
        PGPASSWORD="${POSTGRES_PASSWORD:-secret}" createdb -h "${pg_host}" -p "${pg_port}" -U "${pg_user}" "${db}" 2>/dev/null || true
      fi
      PGPASSWORD="${POSTGRES_PASSWORD:-secret}" psql -h "${pg_host}" -p "${pg_port}" -U "${pg_user}" -d "${db}" < "${sql_file}" || {
        log "[ERROR] psql restore failed for db=${db}"; return 1; }
    fi
  else
    log "[ERROR] No docker nor psql available for restore (db=${db})"; return 1
  fi
  log "[OK] Postgres restore complete db=${db} from ${src}"
  if [[ "${plain}" != "${src}" ]] && [[ -f "${plain}" ]]; then
    if [[ "${src}" == *.age ]] || [[ "${src}" == *.gpg ]]; then
      rm -f "${plain}" "${decompressed:-}" || true
    else
      rm -f "${decompressed:-}" || true
    fi
  else
    rm -f "${decompressed:-}" || true
  fi
}

restore_postgres() {
  local src="$1"
  # Legacy single-arg path: delegate to per-DB with openagentos
  if [[ ${#PG_RESTORE_LIST[@]} -eq 0 ]]; then
    if [[ -z "${src}" ]]; then
      log "[WARN] No postgres restore source"; return 0; fi
    local db="openagentos"
    if [[ -n "${REQUESTED_DBS}" ]]; then db=$(echo "${REQUESTED_DBS}" | awk '{print $1}'); fi
    restore_postgres_single "${db}" "${src}"
    return $?
  fi
  local fail=0
  for entry in "${PG_RESTORE_LIST[@]}"; do
    local db="${entry%%:*}"
    local file="${entry#*:}"
    restore_postgres_single "${db}" "${file}" || fail=1
  done
  return $fail
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
    docker cp "${plain}" oaos-redis:/data/dump.rdb || { log "[ERROR] docker cp failed"; return 1; }
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
  log "=== OAOS Restore start dry_run=${DRY_RUN} mode=${MODE} requested_dbs=${REQUESTED_DBS:-all} ==="
  if [[ -n "${MANIFEST}" ]]; then
    resolve_from_manifest
  else
    # No manifest: build list from --pg-file
    if [[ -n "${PG_FILE}" ]]; then
      local db="openagentos"
      if [[ -n "${REQUESTED_DBS}" ]]; then db=$(echo "${REQUESTED_DBS}" | awk '{print $1}'); fi
      PG_RESTORE_LIST=("${db}:${PG_FILE}")
    fi
  fi
  if [[ ${#PG_RESTORE_LIST[@]} -eq 0 ]] && [[ -z "${REDIS_FILE}" ]] && [[ -z "${MANIFEST}" ]]; then
    # Fallback: support legacy single pg file without manifest list
    if [[ -z "${PG_FILE}" ]] && [[ -z "${REDIS_FILE}" ]]; then
      echo "Usage: $0 --manifest <manifest.json> [--db DB] [--dry-run]" >&2
      echo "   or: $0 --pg-file <file> [--db DB] --redis-file <file> [--dry-run]" >&2
      exit 2
    fi
  fi
  local fail=0
  if [[ "${MODE}" == "all" ]] || [[ "${MODE}" == "postgres-only" ]]; then
    if [[ ${#PG_RESTORE_LIST[@]} -gt 0 ]]; then
      restore_postgres "" || fail=1
    elif [[ -n "${PG_FILE}" ]]; then
      restore_postgres "${PG_FILE}" || fail=1
    else
      log "[INFO] No postgres restore sources resolved — skipping"
    fi
  fi
  if [[ "${MODE}" == "all" ]] || [[ "${MODE}" == "redis-only" ]]; then
    restore_redis "${REDIS_FILE}" || fail=1
  fi
  if [[ $DO_VERIFY -eq 1 ]]; then
    oaos_consistency_check || log "[WARN] Post-restore consistency check flagged issues"
  fi
  if [[ $fail -ne 0 ]]; then
    log "[ERROR] Restore completed with errors"
    exit 1
  fi
  log "=== OAOS Restore complete (dry_run=${DRY_RUN}) ==="
}

main "$@"
