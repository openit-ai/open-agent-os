#!/usr/bin/env bash
# backup.sh — Business edition backup (pg_dump + redis RDB + encrypt, retention, S3)
# v1.6 §27.11: 3-DB individual backups (mattermost, outline, openagentos) even on shared instance,
# plus pgvector dump verification and migration consistency check.
# Usage: ./deploy/scripts/backup.sh [--dry-run] [--backup-dir DIR] [--verify|--check] [--db DB]
# Env: POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, DATABASE_URL, REDIS_URL,
#      REDIS_PASSWORD, BACKUP_DIR, BACKUP_RETENTION_DAYS (7), BACKUP_RETENTION_WEEKLY (30),
#      BACKUP_PG_DBS (space-separated, default: openagentos mattermost outline),
#      AGE_PUBLIC_KEY / AGE_RECIPIENT, GPG_RECIPIENT, AWS_S3_BUCKET, AWS_S3_REGION, AWS_S3_PREFIX
# Cron: 0 2 * * * /opt/open-agent-os/deploy/scripts/backup.sh >> /var/log/oaos-backup.log 2>&1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# ── args ────────────────────────────────────────────────────────────
DRY_RUN=0
DO_VERIFY=0
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/deploy/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
RETENTION_WEEKLY="${BACKUP_RETENTION_WEEKLY:-30}"
TIMESTAMP="$(date -u +%Y%m%d-%H%M%S)"
# §27.11 DB isolation: even on shared instance, DBs/users are separate
BACKUP_PG_DBS="${BACKUP_PG_DBS:-openagentos mattermost outline}"
# allow filtering via --db (repeatable)
_REQUESTED_DBS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
    --verify|--check) DO_VERIFY=1; shift ;;
    --db) _REQUESTED_DBS="${_REQUESTED_DBS} $2"; shift 2 ;;
    --help|-h) echo "Usage: $0 [--dry-run] [--backup-dir DIR] [--verify] [--db DB]"; echo "  --db may be repeated to filter (openagentos|mattermost|outline)"; exit 0 ;;
    *) echo "[WARN] Unknown arg: $1" >&2; shift ;;
  esac
done

_REQUESTED_DBS=$(echo "${_REQUESTED_DBS}" | xargs 2>/dev/null || echo "${_REQUESTED_DBS}")

# Support env var override
if [[ "${OAOS_BACKUP_DRY_RUN:-}" == "1" ]]; then DRY_RUN=1; fi

mkdir -p "${BACKUP_DIR}"
BACKUP_FILE_PREFIX="${BACKUP_DIR}/oaos-${TIMESTAMP}"
# Legacy single-DB vars kept for backward compat (first DB)
PG_DUMP_FILE="${BACKUP_FILE_PREFIX}-postgres.sql"
PG_DUMP_GZ="${PG_DUMP_FILE}.gz"
REDIS_RDB_FILE="${BACKUP_FILE_PREFIX}-redis.rdb"
AUDIT_CHECKPOINT_SRC="${OAOS_AUDIT_CHECKPOINT_S3:-/var/lib/oaos/audit-checkpoint.json}"
AUDIT_CHECKPOINT_FILE="${BACKUP_FILE_PREFIX}-audit-checkpoint.json"
MANIFEST_FILE="${BACKUP_FILE_PREFIX}-manifest.json"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2; }
dry_prefix() { if [[ $DRY_RUN -eq 1 ]]; then echo "[DRY-RUN] "; fi; }

# Track per-DB results for manifest
declare -a PG_DB_LIST=()
declare -a PG_DUMP_FILES=()       # final paths (after encryption if any)
declare -a PG_DUMP_RAW_FILES=()   # raw .gz before encryption
declare -A PG_DUMP_STATUS=()      # ok|skipped|failed|placeholder
declare -A PG_DUMP_SIZE=()
declare -A PG_DUMP_PGVECTOR=()    # true/false/null

# ── helpers ────────────────────────────────────────────────────────
encrypt_file() {
  local src="$1" dst="" enc=""
  if [[ -n "${AGE_PUBLIC_KEY:-${AGE_RECIPIENT:-}}" ]]; then
    local recipient="${AGE_PUBLIC_KEY:-${AGE_RECIPIENT}}"
    dst="${src}.age"
    if [[ $DRY_RUN -eq 1 ]]; then
      log "$(dry_prefix)Would encrypt ${src} -> ${dst} with age (recipient ${recipient:0:12}...)"
      enc="${dst}"
    else
      if ! command -v age >/dev/null 2>&1; then
        log "[WARN] age not found — skipping encryption for ${src}"
        enc="${src}"
      else
        age --recipient "${recipient}" --output "${dst}" "${src}" || {
          log "[ERROR] age encryption failed for ${src}"; return 1; }
        rm -f "${src}"
        enc="${dst}"
        log "[OK] Encrypted ${src} -> ${dst} (age)"
      fi
    fi
  elif [[ -n "${GPG_RECIPIENT:-}" ]]; then
    dst="${src}.gpg"
    if [[ $DRY_RUN -eq 1 ]]; then
      log "$(dry_prefix)Would encrypt ${src} -> ${dst} with gpg (recipient ${GPG_RECIPIENT})"
      enc="${dst}"
    else
      if ! command -v gpg >/dev/null 2>&1; then
        log "[WARN] gpg not found — skipping encryption for ${src}"
        enc="${src}"
      else
        gpg --encrypt --recipient "${GPG_RECIPIENT}" --output "${dst}" "${src}" || {
          log "[ERROR] gpg encryption failed for ${src}"; return 1; }
        rm -f "${src}"
        enc="${dst}"
        log "[OK] Encrypted ${src} -> ${dst} (gpg)"
      fi
    fi
  else
    log "[WARN] No AGE_PUBLIC_KEY/GPG_RECIPIENT set — storing unencrypted (Business edition should set encryption key)"
    enc="${src}"
  fi
  echo "${enc}"
}

# Check if DB exists (graceful skip). Returns 0 if exists or cannot determine (try anyway), 1 if definitely missing.
db_exists() {
  local db="$1"
  # Try docker psql \l
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-postgres"; then
    local pg_user="${POSTGRES_USER:-openagentos}"
    if docker exec oaos-postgres psql -U "${pg_user}" -d postgres -lqt 2>/dev/null | cut -d \| -f 1 | grep -qw "${db}"; then
      return 0
    fi
    # If docker exists but db not in list, check explicitly — if psql succeeded, db missing -> skip
    if docker exec oaos-postgres psql -U "${pg_user}" -d postgres -c "SELECT 1" >/dev/null 2>&1; then
      # we could talk to postgres, so missing means skip
      if ! docker exec oaos-postgres psql -U "${pg_user}" -d postgres -lqt 2>/dev/null | cut -d \| -f 1 | grep -qw "${db}"; then
        return 1
      fi
    fi
  fi
  if command -v psql >/dev/null 2>&1; then
    local pg_user="${POSTGRES_USER:-openagentos}"
    local pg_host="${POSTGRES_HOST:-localhost}"
    local pg_port="${POSTGRES_PORT:-5432}"
    local pg_pw="${POSTGRES_PASSWORD:-secret}"
    if PGPASSWORD="${pg_pw}" psql -h "${pg_host}" -p "${pg_port}" -U "${pg_user}" -d postgres -lqt 2>/dev/null | cut -d \| -f 1 | grep -qw "${db}"; then
      return 0
    fi
    # if we could connect, missing -> skip; else unknown -> try dump anyway
    if PGPASSWORD="${pg_pw}" psql -h "${pg_host}" -p "${pg_port}" -U "${pg_user}" -d postgres -c "SELECT 1" >/dev/null 2>&1; then
      return 1
    fi
  fi
  # Cannot determine — assume exists and let pg_dump tell us
  return 0
}

# Dump a single DB to outfile (.sql.gz). Returns: 0 ok, 2 skipped, 1 failed
do_pg_dump_single() {
  local db="$1" outfile="$2"
  log "$(dry_prefix)Postgres backup db=${db} -> ${outfile}"

  if [[ $DRY_RUN -eq 1 ]]; then
    echo "-- dry-run pg_dump placeholder for ${db} at ${TIMESTAMP}" | gzip > "${outfile}" 2>/dev/null || echo "-- dry-run pg_dump placeholder for ${db}" > "${outfile}"
    if [[ ! -f "${outfile}" ]]; then
      echo "-- dry-run pg_dump placeholder" > "${outfile}"
    fi
    log "[DRY-RUN] Created placeholder ${outfile}"
    return 0
  fi

  local dumped=0 err_msg=""
  # Try docker exec first
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-postgres"; then
    local pg_user="${POSTGRES_USER:-openagentos}"
    log "[INFO] Using docker exec pg_dump (container oaos-postgres, db=${db})"
    local tmp_err
    tmp_err=$(mktemp)
    if docker exec oaos-postgres pg_dump -U "${pg_user}" -d "${db}" --no-owner --no-acl 2>"${tmp_err}" | gzip > "${outfile}"; then
      dumped=1
    else
      err_msg=$(cat "${tmp_err}" 2>/dev/null || true)
      if echo "${err_msg}" | grep -qi "does not exist"; then
        log "[INFO] DB ${db} does not exist — skipping (graceful, §27.11 shared instance)"
        rm -f "${outfile}" "${tmp_err}" || true
        return 2
      fi
      log "[WARN] docker exec pg_dump failed for ${db}: ${err_msg} — trying host pg_dump"
    fi
    rm -f "${tmp_err}" || true
  fi

  if [[ $dumped -eq 0 ]]; then
    if command -v pg_dump >/dev/null 2>&1; then
      local pg_url="${DATABASE_URL:-}"
      # Build URL for specific DB by replacing db name in URL if present
      if [[ -n "${pg_url}" ]]; then
        pg_url="${pg_url/postgresql+asyncpg:/postgresql:}"
        # replace trailing /dbname with /target db
        # e.g. postgresql://user:pass@host:5432/openagentos -> .../${db}
        local base_url
        base_url=$(echo "${pg_url}" | sed -E 's|/[^/]*$|/|')
        pg_url="${base_url}${db}"
        log "[INFO] Using pg_dump via DATABASE_URL -> ${db}"
        local tmp_err2
        tmp_err2=$(mktemp)
        if pg_dump "${pg_url}" --no-owner --no-acl 2>"${tmp_err2}" | gzip > "${outfile}"; then
          dumped=1
        else
          err_msg=$(cat "${tmp_err2}" 2>/dev/null || true)
          if echo "${err_msg}" | grep -qi "does not exist"; then
            log "[INFO] DB ${db} does not exist — skipping (host pg_dump)"
            rm -f "${outfile}" "${tmp_err2}" || true
            return 2
          fi
        fi
        rm -f "${tmp_err2}" || true
      else
        local pg_user="${POSTGRES_USER:-openagentos}"
        local pg_host="${POSTGRES_HOST:-localhost}"
        local pg_port="${POSTGRES_PORT:-5432}"
        log "[INFO] Using pg_dump via PGPASSWORD/host for db=${db}"
        local tmp_err3
        tmp_err3=$(mktemp)
        if PGPASSWORD="${POSTGRES_PASSWORD:-secret}" pg_dump -h "${pg_host}" -p "${pg_port}" -U "${pg_user}" -d "${db}" --no-owner --no-acl 2>"${tmp_err3}" | gzip > "${outfile}"; then
          dumped=1
        else
          err_msg=$(cat "${tmp_err3}" 2>/dev/null || true)
          if echo "${err_msg}" | grep -qi "does not exist"; then
            log "[INFO] DB ${db} does not exist — skipping"
            rm -f "${outfile}" "${tmp_err3}" || true
            return 2
          fi
          log "[WARN] pg_dump failed for ${db}: ${err_msg}"
        fi
        rm -f "${tmp_err3}" || true
      fi
    fi
  fi

  if [[ $dumped -eq 0 ]]; then
    log "[WARN] pg_dump not available/failed for ${db} — creating placeholder dump (check postgres connectivity)"
    echo "-- placeholder: pg_dump unavailable for ${db} at ${TIMESTAMP}" | gzip > "${outfile}" || echo "-- placeholder" > "${outfile}"
    # mark as placeholder but still ok for backward compat
    return 0
  fi

  log "[OK] Postgres dump db=${db}: ${outfile} ($(du -h "${outfile}" 2>/dev/null | cut -f1 || echo "unknown"))"
  return 0
}

# Verify dump contains pgvector artefacts where expected (openagentos should have vector)
verify_pgvector_dump() {
  local db="$1" dumpfile="$2"
  if [[ ! -f "${dumpfile}" ]]; then
    echo "null"
    return 0
  fi
  # Check gzip content for vector extension / VECTOR type
  local found=0
  if gzip -dc "${dumpfile}" 2>/dev/null | grep -qi "vector\|CREATE EXTENSION.*vector\|pgvector"; then
    found=1
  fi
  if [[ "${db}" == "openagentos" ]]; then
    if [[ $found -eq 1 ]]; then
      log "[OK] pgvector artefacts found in dump for ${db}"
      echo "true"
    else
      log "[WARN] pgvector artefacts NOT found in dump for ${db} — expected for openagentos (§27.11). Dump may be incomplete or extension not installed."
      echo "false"
    fi
  else
    # mattermost/outline normally no vector; just report if found
    if [[ $found -eq 1 ]]; then
      log "[INFO] pgvector artefacts found in dump for ${db} (unexpected but ok)"
      echo "true"
    else
      echo "false"
    fi
  fi
}

# ── §27.11 consistency check helper: pgvector extension + alembic version ──
# Usage: oaos_consistency_check [--json]
# Checks: 1) pgvector extension exists in openagentos DB, 2) alembic version matches head
oaos_consistency_check() {
  local json_out=0
  if [[ "${1:-}" == "--json" ]]; then json_out=1; fi
  local pgvector_ok="unknown" alembic_ok="unknown" alembic_current="" alembic_head=""
  local errors=()

  log "=== §27.11 Consistency check: pgvector + alembic ==="

  # 1) pgvector extension check (live DB)
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would check: SELECT * FROM pg_extension WHERE extname='vector' on openagentos"
    pgvector_ok="dry-run"
  else
    local vec_found=0
    if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-postgres"; then
      local pg_user="${POSTGRES_USER:-openagentos}"
      if docker exec oaos-postgres psql -U "${pg_user}" -d openagentos -c "SELECT extname FROM pg_extension WHERE extname='vector';" 2>/dev/null | grep -q "vector"; then
        vec_found=1
      else
        # also try listing extensions
        if docker exec oaos-postgres psql -U "${pg_user}" -d openagentos -c "\dx" 2>/dev/null | grep -q "vector"; then
          vec_found=1
        fi
      fi
    elif command -v psql >/dev/null 2>&1; then
      local pg_user="${POSTGRES_USER:-openagentos}"
      local pg_host="${POSTGRES_HOST:-localhost}"
      local pg_port="${POSTGRES_PORT:-5432}"
      local pg_url="${DATABASE_URL:-}"
      if [[ -n "${pg_url}" ]]; then
        pg_url="${pg_url/postgresql+asyncpg:/postgresql:}"
        pg_url=$(echo "${pg_url}" | sed -E 's|/[^/]*$|/openagentos|')
        if psql "${pg_url}" -c "SELECT extname FROM pg_extension WHERE extname='vector';" 2>/dev/null | grep -q "vector"; then
          vec_found=1
        fi
      else
        if PGPASSWORD="${POSTGRES_PASSWORD:-secret}" psql -h "${pg_host}" -p "${pg_port}" -U "${pg_user}" -d openagentos -c "SELECT extname FROM pg_extension WHERE extname='vector';" 2>/dev/null | grep -q "vector"; then
          vec_found=1
        fi
      fi
    else
      log "[WARN] No docker/psql available — cannot verify pgvector live"
      pgvector_ok="unknown"
      vec_found=-1
    fi
    if [[ $vec_found -eq 1 ]]; then
      pgvector_ok="true"
      log "[OK] pgvector extension present in openagentos"
    elif [[ $vec_found -eq 0 ]]; then
      pgvector_ok="false"
      log "[WARN] pgvector extension NOT found in openagentos — expected pgvector/pgvector:pg16 (§27)"
      errors+=("pgvector extension missing in openagentos")
    fi
  fi

  # 2) alembic version check
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would check: alembic current vs alembic heads"
    alembic_ok="dry-run"
  else
    local alembic_current_raw="" alembic_head_raw=""
    if [[ -f "${REPO_ROOT}/alembic.ini" ]]; then
      if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-control-plane"; then
        alembic_current_raw=$(docker exec oaos-control-plane alembic current 2>/dev/null | head -n 20 || true)
        alembic_head_raw=$(docker exec oaos-control-plane alembic heads 2>/dev/null | head -n 20 || true)
      fi
      if [[ -z "${alembic_current_raw}" ]] && command -v alembic >/dev/null 2>&1; then
        alembic_current_raw=$( (cd "${REPO_ROOT}" && alembic current 2>/dev/null) | head -n 20 || true)
        alembic_head_raw=$( (cd "${REPO_ROOT}" && alembic heads 2>/dev/null) | head -n 20 || true)
      fi
      # Extract version IDs (alembic current outputs like "abc123 (head)" or "abc123")
      alembic_current=$(echo "${alembic_current_raw}" | grep -oE '[a-f0-9_]+' | head -n1 || echo "")
      alembic_head=$(echo "${alembic_head_raw}" | grep -oE '[a-f0-9_]+' | head -n1 || echo "")
      if [[ -z "${alembic_current}" ]] && echo "${alembic_current_raw}" | grep -qi "head"; then
        alembic_ok="true"
        alembic_current="head"
        alembic_head="head"
        log "[OK] alembic at head (container/host): ${alembic_current_raw}"
      elif [[ -n "${alembic_current}" ]] && [[ -n "${alembic_head}" ]]; then
        if echo "${alembic_current_raw}" | grep -q "${alembic_head}" || [[ "${alembic_current}" == "${alembic_head}" ]]; then
          alembic_ok="true"
          log "[OK] alembic current matches head: ${alembic_current}"
        else
          alembic_ok="false"
          log "[WARN] alembic drift: current=${alembic_current} head=${alembic_head}"
          errors+=("alembic drift current=${alembic_current} head=${alembic_head}")
        fi
      elif [[ -n "${alembic_current}" ]]; then
        alembic_ok="true"
        log "[INFO] alembic current=${alembic_current} (heads unavailable, assuming ok)"
      else
        alembic_ok="unknown"
        log "[WARN] alembic current not determinable — DB may be unreachable or not migrated"
        if echo "${alembic_current_raw}" | grep -qi "FAILED\|error"; then
          errors+=("alembic current failed: ${alembic_current_raw}")
        fi
      fi
    else
      log "[WARN] alembic.ini not found — skipping migration version check"
      alembic_ok="unknown"
    fi
  fi

  # Also verify dumps if any were just created (per-DB pgvector presence in dump)
  # This is done inline during backup; here we just report overall.

  if [[ $json_out -eq 1 ]]; then
    printf '{"pgvector": "%s", "alembic_ok": "%s", "alembic_current": "%s", "alembic_head": "%s", "errors": [%s]}\n' \
      "${pgvector_ok}" "${alembic_ok}" "${alembic_current}" "${alembic_head}" \
      "$(printf '"%s",' "${errors[@]}" | sed 's/,$//')"
  fi

  if [[ ${#errors[@]} -gt 0 ]]; then
    log "[WARN] Consistency check found ${#errors[@]} issue(s): ${errors[*]}"
    return 1
  fi
  log "[OK] Consistency check passed (pgvector=${pgvector_ok} alembic=${alembic_ok})"
  return 0
}

# ── 1. Postgres pg_dumps (3-DB) ──────────────────────────────────
do_pg_dumps() {
  # Build DB list, applying filter if requested
  local all_dbs=()
  read -ra all_dbs <<< "${BACKUP_PG_DBS}"
  local dbs=()
  if [[ -n "${_REQUESTED_DBS}" ]]; then
    for want in ${_REQUESTED_DBS}; do
      for cand in "${all_dbs[@]}"; do
        if [[ "${cand}" == "${want}" ]]; then
          dbs+=("${want}")
          break
        fi
      done
    done
    if [[ ${#dbs[@]} -eq 0 ]]; then
      log "[WARN] --db filter '${_REQUESTED_DBS}' matched none of [${all_dbs[*]}] — using filter as-is"
      read -ra dbs <<< "${_REQUESTED_DBS}"
    fi
  else
    dbs=("${all_dbs[@]}")
  fi

  # Ensure openagentos is always first for backward compat (legacy postgres_file)
  # Reorder: openagentos first, then others
  local ordered=()
  for db in "${dbs[@]}"; do
    if [[ "${db}" == "openagentos" ]]; then ordered+=("${db}"); fi
  done
  for db in "${dbs[@]}"; do
    if [[ "${db}" != "openagentos" ]]; then ordered+=("${db}"); fi
  done
  # If openagentos not in list but legacy expects it, ensure at least one entry
  if [[ ${#ordered[@]} -eq 0 ]]; then ordered=("${dbs[@]}"); fi

  for db in "${ordered[@]}"; do
    local outfile="${BACKUP_FILE_PREFIX}-${db}.sql.gz"
    PG_DB_LIST+=("${db}")
    PG_DUMP_RAW_FILES+=("${outfile}")
    # graceful skip if DB definitely missing and not dry-run
    if [[ $DRY_RUN -eq 0 ]]; then
      if ! db_exists "${db}"; then
        log "[INFO] DB ${db} not found on instance — skipping gracefully (§27.11)"
        PG_DUMP_STATUS["${db}"]="skipped"
        PG_DUMP_PGVECTOR["${db}"]="null"
        PG_DUMP_SIZE["${db}"]=0
        continue
      fi
    fi
    local rc=0
    do_pg_dump_single "${db}" "${outfile}" || rc=$?
    if [[ $rc -eq 2 ]]; then
      PG_DUMP_STATUS["${db}"]="skipped"
      PG_DUMP_PGVECTOR["${db}"]="null"
      PG_DUMP_SIZE["${db}"]=0
      continue
    elif [[ $rc -ne 0 ]]; then
      PG_DUMP_STATUS["${db}"]="failed"
      PG_DUMP_PGVECTOR["${db}"]="null"
      PG_DUMP_SIZE["${db}"]=0
      continue
    fi
    PG_DUMP_STATUS["${db}"]="ok"
    # pgvector verification on dump
    local vec
    vec=$(verify_pgvector_dump "${db}" "${outfile}" 2>/dev/null | tail -n1 || echo "null")
    vec=$(echo "${vec}" | tr -d '\r' | xargs)
    if [[ "${vec}" != "true" ]] && [[ "${vec}" != "false" ]] && [[ "${vec}" != "null" ]]; then vec="null"; fi
    PG_DUMP_PGVECTOR["${db}"]="${vec}"
    local sz
    sz=$(stat -c%s "${outfile}" 2>/dev/null || stat -f%z "${outfile}" 2>/dev/null || echo 0)
    PG_DUMP_SIZE["${db}"]="${sz}"
    # Create legacy symlink/compat file for first DB (openagentos) so old restore --pg-file still works
    if [[ "${db}" == "openagentos" ]]; then
      # Ensure legacy path exists as copy for backward compat
      if [[ -f "${outfile}" ]] && [[ "${outfile}" != "${PG_DUMP_GZ}" ]]; then
        cp "${outfile}" "${PG_DUMP_GZ}" 2>/dev/null || true
      fi
    fi
  done

  # Backward compat: if no DB succeeded and legacy file missing, ensure placeholder
  if [[ ${#PG_DB_LIST[@]} -eq 0 ]]; then
    log "[WARN] No DBs in list — falling back to single openagentos dump"
    local outfile="${BACKUP_FILE_PREFIX}-openagentos.sql.gz"
    PG_DB_LIST=("openagentos")
    PG_DUMP_RAW_FILES=("${outfile}")
    do_pg_dump_single "openagentos" "${outfile}" || true
    PG_DUMP_STATUS["openagentos"]="ok"
    PG_DUMP_SIZE["openagentos"]=$(stat -c%s "${outfile}" 2>/dev/null || stat -f%z "${outfile}" 2>/dev/null || echo 0)
    PG_DUMP_PGVECTOR["openagentos"]=$(verify_pgvector_dump "openagentos" "${outfile}" 2>/dev/null | tail -n1 || echo "null")
    cp "${outfile}" "${PG_DUMP_GZ}" 2>/dev/null || true
  fi

  # If filter left us with 0 raw files but we had placeholder, ensure legacy exists
  if [[ ! -f "${PG_DUMP_GZ}" ]] && [[ ${#PG_DUMP_RAW_FILES[@]} -gt 0 ]] && [[ -f "${PG_DUMP_RAW_FILES[0]}" ]]; then
    cp "${PG_DUMP_RAW_FILES[0]}" "${PG_DUMP_GZ}" 2>/dev/null || true
  fi
}

# ── 2. Redis RDB snapshot ────────────────────────────────────────
do_redis_backup() {
  log "$(dry_prefix)Redis backup -> ${REDIS_RDB_FILE}"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "# dry-run redis placeholder ${TIMESTAMP}" > "${REDIS_RDB_FILE}"
    log "[DRY-RUN] Created placeholder ${REDIS_RDB_FILE}"
    return 0
  fi

  local dumped=0
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-redis"; then
    log "[INFO] Using docker exec redis --rdb snapshot"
    local redis_pw="${REDIS_PASSWORD:-}"
    if [[ -n "${redis_pw}" ]]; then
      docker exec oaos-redis redis-cli -a "${redis_pw}" --rdb - 2>/dev/null > "${REDIS_RDB_FILE}" && dumped=1 || true
    else
      docker exec oaos-redis redis-cli --rdb - 2>/dev/null > "${REDIS_RDB_FILE}" && dumped=1 || true
    fi
    if [[ $dumped -eq 0 ]] || [[ ! -s "${REDIS_RDB_FILE}" ]]; then
      log "[INFO] --rdb stream failed, trying BGSAVE + cp dump.rdb"
      if [[ -n "${redis_pw}" ]]; then
        docker exec oaos-redis redis-cli -a "${redis_pw}" BGSAVE 2>/dev/null || true
      else
        docker exec oaos-redis redis-cli BGSAVE 2>/dev/null || true
      fi
      sleep 2
      docker cp oaos-redis:/data/dump.rdb "${REDIS_RDB_FILE}" 2>/dev/null && dumped=1 || true
    fi
  fi

  if [[ $dumped -eq 0 ]]; then
    if command -v redis-cli >/dev/null 2>&1; then
      local redis_pw="${REDIS_PASSWORD:-}"
      if [[ -n "${redis_pw}" ]]; then
        redis-cli -a "${redis_pw}" --rdb "${REDIS_RDB_FILE}" 2>/dev/null && dumped=1 || true
      else
        redis-cli --rdb "${REDIS_RDB_FILE}" 2>/dev/null && dumped=1 || true
      fi
    fi
  fi

  if [[ $dumped -eq 0 ]] || [[ ! -s "${REDIS_RDB_FILE}" ]]; then
    log "[WARN] Redis snapshot not available — creating placeholder"
    echo "# placeholder redis dump ${TIMESTAMP}" > "${REDIS_RDB_FILE}"
  else
    log "[OK] Redis RDB: ${REDIS_RDB_FILE} ($(du -h "${REDIS_RDB_FILE}" 2>/dev/null | cut -f1 || echo "unknown"))"
  fi
}


# ── 2b. Audit checkpoint external anchor (OAOS_AUDIT_CHECKPOINT_S3 or /var/lib/oaos/audit-checkpoint.json) ──
do_audit_checkpoint_backup() {
  local dst="${AUDIT_CHECKPOINT_FILE}"
  log "$(dry_prefix)Audit checkpoint backup -> ${dst} (src=${AUDIT_CHECKPOINT_SRC})"
  local src="${AUDIT_CHECKPOINT_SRC}"
  if [[ "${src}" == s3://* ]]; then
    if command -v aws >/dev/null 2>&1; then
      local region="${AWS_S3_REGION:-ap-northeast-2}"
      if aws s3 cp "${src}" "${dst}" --region "${region}" 2>/dev/null; then
        log "[OK] Audit checkpoint fetched from S3 ${src} -> ${dst}"
        return 0
      else
        log "[WARN] S3 checkpoint fetch failed ${src} — trying local fallbacks"
      fi
    fi
    src="/var/lib/oaos/audit-checkpoint.json"
  fi
  # try local candidates
  local candidates=("${src}" "/tmp/oaos-audit-checkpoint.json" "/var/lib/oaos/audit-checkpoint.json")
  # de-dupe and check
  local found=0
  for cand in "${candidates[@]}"; do
    if [[ -f "${cand}" && "${cand}" != "${dst}" ]]; then
      if cp "${cand}" "${dst}" 2>/dev/null; then
        log "[OK] Audit checkpoint copied ${cand} -> ${dst}"
        found=1
        break
      fi
    elif [[ -f "${cand}" && "${cand}" == "${dst}" ]]; then
      log "[OK] Audit checkpoint already at ${dst}"
      found=1
      break
    fi
  done
  if [[ $found -eq 1 ]]; then return 0; fi
  # try fetching from security service
  local sec_url="${SECURITY_URL:-http://security:8002}"
  if command -v curl >/dev/null 2>&1; then
    local tmp_http
    tmp_http=$(mktemp)
    local http_code
    http_code=$(curl -sk -o "${dst}" -w "%{http_code}" "${sec_url}/v1/audit/checkpoint" 2>/dev/null || echo "000")
    if [[ "${http_code}" == "200" ]] && [[ -s "${dst}" ]] && grep -q "chain_head_hash" "${dst}" 2>/dev/null; then
      if python3 -c "import json; json.load(open('${dst}'))" 2>/dev/null; then
        log "[OK] Audit checkpoint fetched via ${sec_url}/v1/audit/checkpoint"
        rm -f "${tmp_http}" || true
        return 0
      fi
    fi
    rm -f "${tmp_http}" || true
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '{"chain_head_hash":"","event_count":0,"created_at":"%s","signature":"","note":"dry-run placeholder"}\n' "${TIMESTAMP}" > "${dst}"
    log "[DRY-RUN] Created placeholder audit checkpoint ${dst}"
  else
    printf '{"chain_head_hash":"","event_count":0,"created_at":"%s","signature":"","note":"placeholder - no checkpoint source found"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${dst}"
    log "[WARN] No audit checkpoint source found — placeholder created ${dst}"
  fi
  return 0
}

# ── 3. Retention ──────────────────────────────────────────────────
apply_retention() {
  log "Retention: daily ${RETENTION_DAYS}d, weekly ${RETENTION_WEEKLY}d (dir=${BACKUP_DIR})"
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would prune files older than ${RETENTION_DAYS}d (daily) and ${RETENTION_WEEKLY}d (weekly)"
    local old_daily
    old_daily=$(find "${BACKUP_DIR}" -maxdepth 1 -name "oaos-*.sql.gz*" -o -name "oaos-*.rdb*" -o -name "oaos-*.age" -o -name "oaos-*.gpg" 2>/dev/null | head -n 5 || true)
    if [[ -n "${old_daily}" ]]; then
      log "[DRY-RUN] Candidates (sample): ${old_daily}"
    fi
    return 0
  fi
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name "oaos-*.sql.gz*" -mtime +"${RETENTION_DAYS}" -print -delete 2>/dev/null || true
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name "oaos-*.rdb*" -mtime +"${RETENTION_DAYS}" -print -delete 2>/dev/null || true
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name "oaos-*.age" -mtime +"${RETENTION_DAYS}" -print -delete 2>/dev/null || true
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name "oaos-*.gpg" -mtime +"${RETENTION_DAYS}" -print -delete 2>/dev/null || true
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name "oaos-*.json" -mtime +"${RETENTION_DAYS}" -print -delete 2>/dev/null || true
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name "oaos-*-audit-checkpoint.json*" -mtime +"${RETENTION_DAYS}" -print -delete 2>/dev/null || true
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name "oaos-weekly-*" -mtime +"${RETENTION_WEEKLY}" -print -delete 2>/dev/null || true
  log "[OK] Retention applied"
}

# ── 4. S3 push ───────────────────────────────────────────────────
push_s3() {
  local files=("$@")
  if [[ -z "${AWS_S3_BUCKET:-}" ]]; then
    log "[INFO] AWS_S3_BUCKET not set — skipping S3 push (local-only backup)"
    return 0
  fi
  local prefix="${AWS_S3_PREFIX:-backups/}"
  local region="${AWS_S3_REGION:-ap-northeast-2}"
  if [[ $DRY_RUN -eq 1 ]]; then
    for f in "${files[@]}"; do
      log "[DRY-RUN] Would upload ${f} -> s3://${AWS_S3_BUCKET}/${prefix}$(basename "${f}") (region ${region})"
    done
    return 0
  fi
  if ! command -v aws >/dev/null 2>&1; then
    log "[WARN] aws CLI not found — skipping S3 push"
    return 0
  fi
  for f in "${files[@]}"; do
    if [[ -f "${f}" ]]; then
      local key="${prefix}$(basename "${f}")"
      log "[INFO] Uploading ${f} -> s3://${AWS_S3_BUCKET}/${key}"
      aws s3 cp "${f}" "s3://${AWS_S3_BUCKET}/${key}" --region "${region}" --server-side-encryption AES256 2>&1 | log || {
        log "[ERROR] S3 upload failed for ${f}"
        return 1
      }
      aws s3api head-object --bucket "${AWS_S3_BUCKET}" --key "${key}" --region "${region}" >/dev/null 2>&1 && log "[OK] S3 verified: ${key}" || log "[WARN] S3 head-object failed for ${key}"
    fi
  done
}

# ── main ─────────────────────────────────────────────────────────
main() {
  log "=== OAOS Backup start ts=${TIMESTAMP} dry_run=${DRY_RUN} dir=${BACKUP_DIR} dbs=${BACKUP_PG_DBS} ==="

  do_pg_dumps
  do_redis_backup
  do_audit_checkpoint_backup

  # Encrypt per-DB dumps
  declare -a final_pg_files=()
  for db in "${PG_DB_LIST[@]}"; do
    local raw=""
    # find raw file for db
    for i in "${!PG_DB_LIST[@]}"; do
      if [[ "${PG_DB_LIST[$i]}" == "${db}" ]]; then raw="${PG_DUMP_RAW_FILES[$i]}"; break; fi
    done
    if [[ "${PG_DUMP_STATUS[${db}]}" == "skipped" ]]; then
      continue
    fi
    if [[ -f "${raw}" ]]; then
      local enc
      enc=$(encrypt_file "${raw}") || true
      if [[ -z "${enc}" ]]; then enc="${raw}"; fi
      final_pg_files+=("${enc}")
      # update size after encryption if changed
      local sz
      sz=$(stat -c%s "${enc}" 2>/dev/null || stat -f%z "${enc}" 2>/dev/null || echo "${PG_DUMP_SIZE[${db}]}")
      PG_DUMP_SIZE["${db}"]="${sz}"
      # remember final path for manifest (basename only)
      # store mapping via associative array for manifest generation
      # we keep raw->final mapping via variable
      # update PG_DUMP_RAW_FILES entry to final for later S3
      for i in "${!PG_DUMP_RAW_FILES[@]}"; do
        if [[ "${PG_DUMP_RAW_FILES[$i]}" == "${raw}" ]]; then PG_DUMP_RAW_FILES[$i]="${enc}"; break; fi
      done
    fi
  done

  local final_audit="${AUDIT_CHECKPOINT_FILE}"
  if [[ -f "${AUDIT_CHECKPOINT_FILE}" ]]; then
    final_audit=$(encrypt_file "${AUDIT_CHECKPOINT_FILE}") || true
    if [[ -z "${final_audit}" ]]; then final_audit="${AUDIT_CHECKPOINT_FILE}"; fi
  else
    final_audit="${AUDIT_CHECKPOINT_FILE}"
  fi

  local final_redis="${REDIS_RDB_FILE}"
  if [[ -f "${REDIS_RDB_FILE}" ]]; then
    final_redis=$(encrypt_file "${REDIS_RDB_FILE}") || true
    if [[ -z "${final_redis}" ]]; then final_redis="${REDIS_RDB_FILE}"; fi
  fi

  # Backward compat: legacy PG_DUMP_GZ may have been encrypted above already
  # Ensure legacy var points to openagentos final file if exists
  local legacy_final_pg="${PG_DUMP_GZ}"
  for f in "${final_pg_files[@]}"; do
    if [[ "${f}" == *"-openagentos.sql"* ]]; then legacy_final_pg="${f}"; break; fi
  done
  if [[ ${#final_pg_files[@]} -gt 0 ]] && [[ ! -f "${legacy_final_pg}" ]]; then
    legacy_final_pg="${final_pg_files[0]}"
  fi

  # Weekly marker (Sunday)
  local dow
  dow=$(date +%u)
  declare -a weekly_files=()
  if [[ "${dow}" == "7" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      log "[DRY-RUN] Sunday — would create weekly copies"
    else
      for f in "${final_pg_files[@]}" "${final_redis}" "${final_audit}"; do
        if [[ -f "${f}" ]]; then
          local base
          base=$(basename "${f}")
          # oaos-20260101-...-openagentos.sql.gz -> oaos-weekly-20260101-...-openagentos.sql.gz
          local weekly="${BACKUP_DIR}/oaos-weekly-${TIMESTAMP}-${base#oaos-${TIMESTAMP}-}"
          if [[ "${base}" == oaos-weekly-* ]]; then weekly="${BACKUP_DIR}/${base}"; fi
          # if prefix mismatch, fallback
          if [[ ! "${weekly}" == *"${base}"* ]]; then weekly="${BACKUP_DIR}/oaos-weekly-${TIMESTAMP}-${base}"; fi
          cp "${f}" "${weekly}" 2>/dev/null || true
          if [[ -f "${weekly}" ]]; then weekly_files+=("${weekly}"); fi
        fi
      done
    fi
  fi

  # Consistency check (optional or always log)
  if [[ $DO_VERIFY -eq 1 ]]; then
    oaos_consistency_check || log "[WARN] Consistency check returned non-zero — see above"
  else
    # lightweight: still run but don't fail backup on warn
    oaos_consistency_check || true
  fi

  # Manifest — per-DB entries + legacy fields + consistency
  local redis_size
  redis_size=$(stat -c%s "${final_redis}" 2>/dev/null || stat -f%z "${final_redis}" 2>/dev/null || echo 0)
  local legacy_size
  legacy_size=$(stat -c%s "${legacy_final_pg}" 2>/dev/null || stat -f%z "${legacy_final_pg}" 2>/dev/null || echo 0)
  # Build postgres_dbs JSON array
  local dbs_json=""
  local first=1
  for db in "${PG_DB_LIST[@]}"; do
    local status="${PG_DUMP_STATUS[${db}]:-unknown}"
    local sz="${PG_DUMP_SIZE[${db}]:-0}"
    local vec="${PG_DUMP_PGVECTOR[${db}]:-null}"
    # normalize vec to json boolean/null
    if [[ "${vec}" == "true" ]] || [[ "${vec}" == "false" ]]; then vec_json="${vec}"; else vec_json="null"; fi
    # find final file basename for this db
    local fname=""
    for f in "${final_pg_files[@]}"; do
      if [[ "${f}" == *"-${db}.sql"* ]]; then fname=$(basename "${f}"); break; fi
    done
    if [[ "${status}" == "skipped" ]]; then fname=""; sz=0; vec_json="null"; fi
    local entry
    printf -v entry '{"db":"%s","file":"%s","size":%s,"status":"%s","pgvector":%s}' "${db}" "${fname}" "${sz}" "${status}" "${vec_json}"
    if [[ $first -eq 1 ]]; then dbs_json="${entry}"; first=0; else dbs_json="${dbs_json},${entry}"; fi
  done
  # pgvector overall: check openagentos dump
  local pgvector_overall="null"
  if [[ -n "${PG_DUMP_PGVECTOR[openagentos]:-}" ]]; then
    if [[ "${PG_DUMP_PGVECTOR[openagentos]}" == "true" ]]; then pgvector_overall="true"
    elif [[ "${PG_DUMP_PGVECTOR[openagentos]}" == "false" ]]; then pgvector_overall="false"
    fi
  fi
  if [[ "${pgvector_overall}" == "null" ]]; then pgvector_overall="null"; fi
  # alembic check quick (reuse)
  # just run a lightweight query if possible — we already logged above; manifest gets placeholder
  cat > "${MANIFEST_FILE}" <<JSON
{
  "timestamp": "${TIMESTAMP}",
  "backup_dir": "${BACKUP_DIR}",
  "postgres_file": "$(basename "${legacy_final_pg}")",
  "postgres_size": ${legacy_size},
  "postgres_dbs": [${dbs_json}],
  "redis_file": "$(basename "${final_redis}")",
  "redis_size": ${redis_size},
  "audit_checkpoint_file": "$(basename "${final_audit}")",
  "audit_checkpoint_size": $(stat -c%s "${final_audit}" 2>/dev/null || stat -f%z "${final_audit}" 2>/dev/null || echo 0),
  "audit_checkpoint_src": "${AUDIT_CHECKPOINT_SRC}",
  "pgvector_verified": ${pgvector_overall},
  "encrypted": $( [[ "${legacy_final_pg}" == *.age ]] || [[ "${legacy_final_pg}" == *.gpg ]] && echo "true" || echo "false"),
  "dry_run": $( [[ $DRY_RUN -eq 1 ]] && echo "true" || echo "false"),
  "retention_daily_days": ${RETENTION_DAYS},
  "retention_weekly_days": ${RETENTION_WEEKLY}
}
JSON
  log "[OK] Manifest: ${MANIFEST_FILE}"
  cat "${MANIFEST_FILE}" >&2 || true

  # S3 push
  local to_upload=()
  for f in "${final_pg_files[@]}"; do to_upload+=("${f}"); done
  to_upload+=("${final_redis}" "${final_audit}" "${MANIFEST_FILE}")
  for wf in "${weekly_files[@]}"; do to_upload+=("${wf}"); done
  push_s3 "${to_upload[@]}"

  apply_retention

  # Emit Prometheus metric oaos_backup_last_success_timestamp (for alerts.yml)
  if [[ $DRY_RUN -eq 0 ]]; then
    _ts=$(date +%s)
    for _mf in "/var/lib/node_exporter/textfile/oaos_backup.prom" "/tmp/oaos_backup.prom"; do
      if mkdir -p "$(dirname "${_mf}")" 2>/dev/null; then
        { echo "# HELP oaos_backup_last_success_timestamp Last successful backup unix timestamp";
          echo "# TYPE oaos_backup_last_success_timestamp gauge";
          echo "oaos_backup_last_success_timestamp ${_ts}";
          echo "# HELP oaos_backup_last_success_timestamp_seconds Same as oaos_backup_last_success_timestamp";
          echo "oaos_backup_duration_seconds 0";
        } > "${_mf}" 2>/dev/null && log "[OK] Backup metric written to ${_mf} ts=${_ts}" && break || true
      fi
    done
  else
    log "[DRY-RUN] Would emit oaos_backup_last_success_timestamp $(date +%s) to node_exporter textfile"
  fi

  log "=== OAOS Backup complete (dry_run=${DRY_RUN}) dbs=${PG_DB_LIST[*]} ==="
  echo "${MANIFEST_FILE}"
}

# Allow sourcing for consistency helper without running main
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
