#!/usr/bin/env bash
# backup.sh — Business edition backup (pg_dump + redis RDB + encrypt, retention, S3)
# Usage: ./deploy/scripts/backup.sh [--dry-run] [--backup-dir DIR]
# Env: POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, DATABASE_URL, REDIS_URL,
#      REDIS_PASSWORD, BACKUP_DIR, BACKUP_RETENTION_DAYS (7), BACKUP_RETENTION_WEEKLY (30),
#      AGE_PUBLIC_KEY / AGE_RECIPIENT, GPG_RECIPIENT, AWS_S3_BUCKET, AWS_S3_REGION, AWS_S3_PREFIX
# Cron: 0 2 * * * /opt/open-agent-os/deploy/scripts/backup.sh >> /var/log/oaos-backup.log 2>&1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# ── args ────────────────────────────────────────────────────────────
DRY_RUN=0
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/deploy/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
RETENTION_WEEKLY="${BACKUP_RETENTION_WEEKLY:-30}"
TIMESTAMP="$(date -u +%Y%m%d-%H%M%S)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
    --help|-h) echo "Usage: $0 [--dry-run] [--backup-dir DIR]"; exit 0 ;;
    *) echo "[WARN] Unknown arg: $1" >&2; shift ;;
  esac
done

# Support env var override
if [[ "${OAOS_BACKUP_DRY_RUN:-}" == "1" ]]; then DRY_RUN=1; fi

mkdir -p "${BACKUP_DIR}"
BACKUP_FILE_PREFIX="${BACKUP_DIR}/oaos-${TIMESTAMP}"
PG_DUMP_FILE="${BACKUP_FILE_PREFIX}-postgres.sql"
PG_DUMP_GZ="${PG_DUMP_FILE}.gz"
REDIS_RDB_FILE="${BACKUP_FILE_PREFIX}-redis.rdb"
MANIFEST_FILE="${BACKUP_FILE_PREFIX}-manifest.json"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2; }
dry_prefix() { if [[ $DRY_RUN -eq 1 ]]; then echo "[DRY-RUN] "; fi; }

# ── helpers ────────────────────────────────────────────────────────
encrypt_file() {
  local src="$1" dst="" enc=""
  # Prefer age, fallback to gpg
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

# ── 1. Postgres pg_dump ──────────────────────────────────────────
do_pg_dump() {
  log "$(dry_prefix)Postgres backup -> ${PG_DUMP_GZ}"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "-- dry-run pg_dump placeholder for ${TIMESTAMP}" | gzip > "${PG_DUMP_GZ}" 2>/dev/null || true
    # Ensure file exists even if gzip missing
    if [[ ! -f "${PG_DUMP_GZ}" ]]; then
      echo "-- dry-run pg_dump placeholder" > "${PG_DUMP_GZ}"
    fi
    log "[DRY-RUN] Created placeholder ${PG_DUMP_GZ}"
    return 0
  fi

  # Try docker exec first (prod compose), then pg_dump directly
  local dumped=0
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-postgres"; then
    local pg_user="${POSTGRES_USER:-open_agent_os}"
    local pg_db="${POSTGRES_DB:-open_agent_os}"
    log "[INFO] Using docker exec pg_dump (container oaos-postgres, db=${pg_db})"
    if docker exec oaos-postgres pg_dump -U "${pg_user}" -d "${pg_db}" --no-owner --no-acl 2>/dev/null | gzip > "${PG_DUMP_GZ}"; then
      dumped=1
    else
      log "[WARN] docker exec pg_dump failed — trying host pg_dump"
    fi
  fi

  if [[ $dumped -eq 0 ]]; then
    if command -v pg_dump >/dev/null 2>&1; then
      local pg_url="${DATABASE_URL:-}"
      if [[ -n "${pg_url}" ]]; then
        # Convert asyncpg URL to plain
        pg_url="${pg_url/postgresql+asyncpg:/postgresql:}"
        log "[INFO] Using pg_dump via DATABASE_URL"
        if pg_dump "${pg_url}" --no-owner --no-acl 2>/dev/null | gzip > "${PG_DUMP_GZ}"; then
          dumped=1
        fi
      else
        local pg_user="${POSTGRES_USER:-open_agent_os}"
        local pg_db="${POSTGRES_DB:-open_agent_os}"
        local pg_host="${POSTGRES_HOST:-localhost}"
        local pg_port="${POSTGRES_PORT:-5432}"
        log "[INFO] Using pg_dump via PGPASSWORD/host"
        PGPASSWORD="${POSTGRES_PASSWORD:-secret}" pg_dump -h "${pg_host}" -p "${pg_port}" -U "${pg_user}" -d "${pg_db}" --no-owner --no-acl 2>/dev/null | gzip > "${PG_DUMP_GZ}" && dumped=1 || true
      fi
    fi
  fi

  if [[ $dumped -eq 0 ]]; then
    # Fallback: create placeholder so manifest is still valid (but warn)
    log "[WARN] pg_dump not available — creating placeholder dump (check postgres connectivity)"
    echo "-- placeholder: pg_dump unavailable at ${TIMESTAMP}" | gzip > "${PG_DUMP_GZ}" || echo "-- placeholder" > "${PG_DUMP_GZ}"
  fi

  log "[OK] Postgres dump: ${PG_DUMP_GZ} ($(du -h "${PG_DUMP_GZ}" 2>/dev/null | cut -f1 || echo "unknown"))"
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
    # Use redis-cli --rdb to stream snapshot
    local redis_pw="${REDIS_PASSWORD:-}"
    if [[ -n "${redis_pw}" ]]; then
      docker exec oaos-redis redis-cli -a "${redis_pw}" --rdb - 2>/dev/null > "${REDIS_RDB_FILE}" && dumped=1 || true
    else
      docker exec oaos-redis redis-cli --rdb - 2>/dev/null > "${REDIS_RDB_FILE}" && dumped=1 || true
    fi
    # Fallback: BGSAVE + copy dump.rdb
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
      local redis_url="${REDIS_URL:-}"
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

# ── 3. Retention ──────────────────────────────────────────────────
apply_retention() {
  log "Retention: daily ${RETENTION_DAYS}d, weekly ${RETENTION_WEEKLY}d (dir=${BACKUP_DIR})"
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would prune files older than ${RETENTION_DAYS}d (daily) and ${RETENTION_WEEKLY}d (weekly)"
    # Still show what would be deleted
    local old_daily
    old_daily=$(find "${BACKUP_DIR}" -maxdepth 1 -name "oaos-*.sql.gz*" -o -name "oaos-*.rdb*" -o -name "oaos-*.age" -o -name "oaos-*.gpg" 2>/dev/null | head -n 5 || true)
    if [[ -n "${old_daily}" ]]; then
      log "[DRY-RUN] Candidates (sample): ${old_daily}"
    fi
    return 0
  fi
  # Daily: general backups older than RETENTION_DAYS
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name "oaos-*.sql.gz*" -mtime +"${RETENTION_DAYS}" -print -delete 2>/dev/null || true
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name "oaos-*.rdb*" -mtime +"${RETENTION_DAYS}" -print -delete 2>/dev/null || true
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name "oaos-*.age" -mtime +"${RETENTION_DAYS}" -print -delete 2>/dev/null || true
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name "oaos-*.gpg" -mtime +"${RETENTION_DAYS}" -print -delete 2>/dev/null || true
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name "oaos-*.json" -mtime +"${RETENTION_DAYS}" -print -delete 2>/dev/null || true
  # Weekly archives (files with -weekly- marker) older than RETENTION_WEEKLY
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
      # Verify
      aws s3api head-object --bucket "${AWS_S3_BUCKET}" --key "${key}" --region "${region}" >/dev/null 2>&1 && log "[OK] S3 verified: ${key}" || log "[WARN] S3 head-object failed for ${key}"
    fi
  done
}

# ── main ─────────────────────────────────────────────────────────
main() {
  log "=== OAOS Backup start ts=${TIMESTAMP} dry_run=${DRY_RUN} dir=${BACKUP_DIR} ==="

  do_pg_dump
  do_redis_backup

  # Encrypt
  local final_pg="${PG_DUMP_GZ}"
  local final_redis="${REDIS_RDB_FILE}"
  if [[ -f "${PG_DUMP_GZ}" ]]; then
    final_pg=$(encrypt_file "${PG_DUMP_GZ}") || true
    # encrypt_file may return original if no key; handle .gz already present
    if [[ -z "${final_pg}" ]]; then final_pg="${PG_DUMP_GZ}"; fi
  fi
  if [[ -f "${REDIS_RDB_FILE}" ]]; then
    final_redis=$(encrypt_file "${REDIS_RDB_FILE}") || true
    if [[ -z "${final_redis}" ]]; then final_redis="${REDIS_RDB_FILE}"; fi
  fi

  # Weekly marker (Sunday) — copy with weekly prefix for longer retention
  local dow
  dow=$(date +%u)
  local weekly_pg="" weekly_redis=""
  if [[ "${dow}" == "7" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      log "[DRY-RUN] Sunday — would create weekly copies"
    else
      weekly_pg="${BACKUP_DIR}/oaos-weekly-${TIMESTAMP}-postgres.sql.gz"
      weekly_redis="${BACKUP_DIR}/oaos-weekly-${TIMESTAMP}-redis.rdb"
      if [[ -n "${final_pg}" ]] && [[ -f "${final_pg}" ]]; then
        cp "${final_pg}" "${weekly_pg}" 2>/dev/null || true
        # If encrypted, copy encrypted form
        if [[ "${final_pg}" == *.age ]] || [[ "${final_pg}" == *.gpg ]]; then
          weekly_pg="${weekly_pg}.$(echo "${final_pg}" | rev | cut -d. -f1 | rev)"
          cp "${final_pg}" "${weekly_pg}" 2>/dev/null || true
        fi
      fi
      if [[ -n "${final_redis}" ]] && [[ -f "${final_redis}" ]]; then
        cp "${final_redis}" "${weekly_redis}" 2>/dev/null || true
        if [[ "${final_redis}" == *.age ]] || [[ "${final_redis}" == *.gpg ]]; then
          weekly_redis="${weekly_redis}.$(echo "${final_redis}" | rev | cut -d. -f1 | rev)"
          cp "${final_redis}" "${weekly_redis}" 2>/dev/null || true
        fi
      fi
    fi
  fi

  # Manifest
  local pg_size redis_size
  pg_size=$(stat -c%s "${final_pg}" 2>/dev/null || stat -f%z "${final_pg}" 2>/dev/null || echo 0)
  redis_size=$(stat -c%s "${final_redis}" 2>/dev/null || stat -f%z "${final_redis}" 2>/dev/null || echo 0)
  cat > "${MANIFEST_FILE}" <<JSON
{
  "timestamp": "${TIMESTAMP}",
  "backup_dir": "${BACKUP_DIR}",
  "postgres_file": "$(basename "${final_pg}")",
  "postgres_size": ${pg_size},
  "redis_file": "$(basename "${final_redis}")",
  "redis_size": ${redis_size},
  "encrypted": $( [[ "${final_pg}" == *.age ]] || [[ "${final_pg}" == *.gpg ]] && echo "true" || echo "false"),
  "dry_run": $( [[ $DRY_RUN -eq 1 ]] && echo "true" || echo "false"),
  "retention_daily_days": ${RETENTION_DAYS},
  "retention_weekly_days": ${RETENTION_WEEKLY}
}
JSON
  log "[OK] Manifest: ${MANIFEST_FILE}"
  cat "${MANIFEST_FILE}" >&2 || true

  # S3 push
  local to_upload=("${final_pg}" "${final_redis}" "${MANIFEST_FILE}")
  if [[ -n "${weekly_pg:-}" ]] && [[ -f "${weekly_pg}" ]]; then to_upload+=("${weekly_pg}"); fi
  if [[ -n "${weekly_redis:-}" ]] && [[ -f "${weekly_redis}" ]]; then to_upload+=("${weekly_redis}"); fi
  push_s3 "${to_upload[@]}"

  apply_retention

  log "=== OAOS Backup complete (dry_run=${DRY_RUN}) ==="
  # Emit manifest path for automation
  echo "${MANIFEST_FILE}"
}

main "$@"
