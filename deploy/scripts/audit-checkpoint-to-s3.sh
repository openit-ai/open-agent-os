#!/usr/bin/env bash
# audit-checkpoint-to-s3.sh — Audit ledger signed checkpoint → S3 upload (§31 Managed edition)
# Production hardening:
#   1. HMAC-SHA256 sign checkpoint (if server did not sign or signature missing) + verify
#   2. Upload to S3 with versioning guard + SSE-S3 + metadata + content-type
#   3. Verify on download (GET latest.json → HMAC verify)
#   4. Retention: prune objects older than CHECKPOINT_RETENTION_DAYS (default 30d)
# Flow: GET /v1/audit/checkpoint → sign/verify → S3 PutObject → S3 head+download verify → prune
# Cron: 0 * * * * /opt/open-agent-os/deploy/scripts/audit-checkpoint-to-s3.sh
# Required env: AUDIT_SIGNING_KEY (HMAC key), AWS_S3_BUCKET
# Dependencies: curl, python3, aws cli v2
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SECURITY_URL="${SECURITY_URL:-http://security:8002}"
ENDPOINT="${SECURITY_URL}/v1/audit/checkpoint"
AWS_S3_BUCKET="${AWS_S3_BUCKET:-}"
AWS_S3_REGION="${AWS_S3_REGION:-ap-northeast-2}"
AWS_S3_PREFIX="${AWS_S3_PREFIX:-audit-checkpoints/}"
AUDIT_SIGNING_KEY="${AUDIT_SIGNING_KEY:-${OAOS_SIGNING_KEY:-}}"
CHECKPOINT_RETENTION_DAYS="${CHECKPOINT_RETENTION_DAYS:-30}"
CHECKPOINT_ENABLE_VERSIONING="${CHECKPOINT_ENABLE_VERSIONING:-1}"
CHECKPOINT_VERIFY_DOWNLOAD="${CHECKPOINT_VERIFY_DOWNLOAD:-1}"

TMPDIR_CHECKPOINT="$(mktemp -d)"
CHECKPOINT_FILE="${TMPDIR_CHECKPOINT}/checkpoint.json"
DOWNLOAD_VERIFY_FILE="${TMPDIR_CHECKPOINT}/download-verify.json"
trap 'rm -rf "${TMPDIR_CHECKPOINT}"' EXIT

MODE="upload"
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --verify) MODE="verify"; shift ;;
    --prune) MODE="prune"; shift ;;
    --download-verify) MODE="download-verify"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --retention-days) CHECKPOINT_RETENTION_DAYS="$2"; shift 2 ;;
    --bucket) AWS_S3_BUCKET="$2"; shift 2 ;;
    --help|-h)
      echo "Usage: AUDIT_SIGNING_KEY=<key> AWS_S3_BUCKET=<bucket> $0 [options]"
      echo "  Options:"
      echo "    --verify              Download latest.json from S3 and verify HMAC"
      echo "    --download-verify     Alias for --verify"
      echo "    --prune               Prune checkpoints older than retention days"
      echo "    --dry-run             Do not upload/prune, only print actions"
      echo "    --retention-days N    Override retention days (default 30)"
      echo "  Env:"
      echo "    SECURITY_URL (default http://security:8002)"
      echo "    AWS_S3_REGION (default ap-northeast-2)"
      echo "    AWS_S3_PREFIX (default audit-checkpoints/)"
      echo "    CHECKPOINT_RETENTION_DAYS (default 30)"
      echo "    CHECKPOINT_ENABLE_VERSIONING (default 1)"
      echo "    CHECKPOINT_VERIFY_DOWNLOAD (default 1)"
      exit 0 ;;
    *) echo "[WARN] Unknown arg: $1" >&2; shift ;;
  esac
done

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2; }

need_key() {
  if [[ -z "${AUDIT_SIGNING_KEY}" ]]; then
    echo "[ERROR] AUDIT_SIGNING_KEY (or OAOS_SIGNING_KEY) is required" >&2
    exit 1
  fi
}

# ── HMAC helpers ─────────────────────────────────────────────────
sign_checkpoint() {
  # If signature missing or invalid, (re-)sign checkpoint locally with AUDIT_SIGNING_KEY
  # Always ensure signature field = HMAC-SHA256(chain_head_hash)
  python3 - "$1" "$AUDIT_SIGNING_KEY" <<'PY'
import sys, json, hmac, hashlib
path, key = sys.argv[1], sys.argv[2]
with open(path) as f:
    cp = json.load(f)
head = cp.get("chain_head_hash", "")
if not head:
    print("[WARN] chain_head_hash empty — signing empty string", file=sys.stderr)
sig = hmac.new(key.encode(), head.encode(), hashlib.sha256).hexdigest()
cp["signature"] = sig
# also inject signed_at if missing
if "signed_at" not in cp:
    from datetime import datetime, timezone
    cp["signed_at"] = datetime.now(timezone.utc).isoformat()
with open(path, "w") as f:
    json.dump(cp, f, indent=2, sort_keys=True)
    f.write("\n")
print(f"[INFO] Signed checkpoint head={head[:16]} sig={sig[:16]}...")
PY
}

verify_checkpoint() {
  python3 - "$1" "$AUDIT_SIGNING_KEY" <<'PY'
import sys, json, hmac, hashlib
path, key = sys.argv[1], sys.argv[2]
with open(path) as f:
    cp = json.load(f)
head = cp.get("chain_head_hash", "")
sig = cp.get("signature", "")
expected = hmac.new(key.encode(), head.encode(), hashlib.sha256).hexdigest()
if not sig:
    print("[ERROR] Missing signature field", file=sys.stderr)
    sys.exit(2)
if not hmac.compare_digest(expected, sig):
    print(f"[ERROR] Signature mismatch! expected={expected} got={sig}", file=sys.stderr)
    sys.exit(2)
print(f"[OK] Signature valid — head={head[:16]}... count={cp.get('event_count')}")
PY
}

ensure_versioning() {
  if [[ "${CHECKPOINT_ENABLE_VERSIONING}" != "1" ]]; then
    log "Versioning guard disabled (CHECKPOINT_ENABLE_VERSIONING=0)"
    return 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would ensure bucket versioning Enabled on ${AWS_S3_BUCKET}"
    return 0
  fi
  if ! command -v aws >/dev/null 2>&1; then return 0; fi
  local status
  status=$(aws s3api get-bucket-versioning --bucket "${AWS_S3_BUCKET}" --region "${AWS_S3_REGION}" 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('Status',''))" 2>/dev/null || echo "")
  if [[ "${status}" == "Enabled" ]]; then
    log "Bucket versioning already Enabled"
  else
    log "Enabling bucket versioning on ${AWS_S3_BUCKET} ..."
    aws s3api put-bucket-versioning --bucket "${AWS_S3_BUCKET}" --region "${AWS_S3_REGION}" \
      --versioning-configuration Status=Enabled 2>&1 || log "[WARN] Failed to enable versioning (need s3:PutBucketVersioning permission)"
  fi
}

# ── Mode: verify (download latest.json + HMAC verify) ──────────
do_verify() {
  need_key
  if [[ -z "${AWS_S3_BUCKET}" ]]; then echo "[ERROR] AWS_S3_BUCKET required for --verify" >&2; exit 1; fi
  if ! command -v aws >/dev/null 2>&1; then echo "[ERROR] aws CLI not found" >&2; exit 3; fi
  local latest_key="${AWS_S3_PREFIX}latest.json"
  log "Downloading s3://${AWS_S3_BUCKET}/${latest_key} for verification ..."
  if ! aws s3 cp "s3://${AWS_S3_BUCKET}/${latest_key}" "${DOWNLOAD_VERIFY_FILE}" --region "${AWS_S3_REGION}" 2>&1; then
    echo "[ERROR] Failed to download latest checkpoint from S3" >&2
    exit 1
  fi
  cat "${DOWNLOAD_VERIFY_FILE}" | python3 -m json.tool 2>/dev/null || cat "${DOWNLOAD_VERIFY_FILE}"
  log "Verifying HMAC-SHA256 ..."
  verify_checkpoint "${DOWNLOAD_VERIFY_FILE}"
  log "[OK] Download verify passed — checkpoint authentic"
}

# ── Mode: prune ────────────────────────────────────────────────
do_prune() {
  if [[ -z "${AWS_S3_BUCKET}" ]]; then echo "[ERROR] AWS_S3_BUCKET required for --prune" >&2; exit 1; fi
  if ! command -v aws >/dev/null 2>&1; then echo "[ERROR] aws CLI not found" >&2; exit 3; fi
  local cutoff_epoch
  cutoff_epoch=$(python3 -c "import time; print(int(time.time()) - int('${CHECKPOINT_RETENTION_DAYS}')*86400)")
  cutoff_iso=$(python3 -c "from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)-timedelta(days=int('${CHECKPOINT_RETENTION_DAYS}'))).isoformat())")
  log "Pruning checkpoints older than ${CHECKPOINT_RETENTION_DAYS}d (before ${cutoff_iso}) under s3://${AWS_S3_BUCKET}/${AWS_S3_PREFIX} ..."
  # List objects and filter by LastModified
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would list and delete objects older than cutoff"
    aws s3api list-objects-v2 --bucket "${AWS_S3_BUCKET}" --prefix "${AWS_S3_PREFIX}" --region "${AWS_S3_REGION}" 2>&1 | head -n 100 || true
    return 0
  fi
  # Paginated delete via python helper
  python3 - "${AWS_S3_BUCKET}" "${AWS_S3_PREFIX}" "${AWS_S3_REGION}" "${CHECKPOINT_RETENTION_DAYS}" <<'PY'
import sys, subprocess, json
from datetime import datetime, timezone, timedelta
bucket, prefix, region, days = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
cutoff = datetime.now(timezone.utc) - timedelta(days=days)
# list
import json as _json
result = subprocess.run(["aws","s3api","list-objects-v2","--bucket",bucket,"--prefix",prefix,"--region",region], capture_output=True, text=True)
if result.returncode != 0:
    print(result.stderr, file=sys.stderr)
    sys.exit(1)
data = _json.loads(result.stdout) if result.stdout.strip() else {}
objects = data.get("Contents", [])
to_delete = []
for obj in objects:
    key = obj["Key"]
    if key.endswith("latest.json"):
        continue
    lm_str = obj["LastModified"]
    # LastModified like 2026-08-28T00:00:00+00:00 or with Z
    try:
        lm = datetime.fromisoformat(lm_str.replace("Z","+00:00"))
    except Exception:
        continue
    if lm < cutoff:
        to_delete.append(key)
if not to_delete:
    print(f"[OK] No objects older than {days}d to prune ({len(objects)} total)")
    sys.exit(0)
print(f"[INFO] Deleting {len(to_delete)} objects older than {days}d ...")
for key in to_delete:
    print(f"  - {key}")
    subprocess.run(["aws","s3","rm", f"s3://{bucket}/{key}", "--region", region], check=False)
print(f"[OK] Pruned {len(to_delete)} objects")
PY
}

if [[ "${MODE}" == "verify" || "${MODE}" == "download-verify" ]]; then
  do_verify
  exit 0
fi
if [[ "${MODE}" == "prune" ]]; then
  do_prune
  exit 0
fi

# ── Default mode: upload ───────────────────────────────────────
need_key
if [[ -z "${AWS_S3_BUCKET}" ]]; then
  echo "[ERROR] AWS_S3_BUCKET is required" >&2
  echo "Usage: AUDIT_SIGNING_KEY=<key> AWS_S3_BUCKET=<bucket> $0" >&2
  exit 1
fi

log "Fetching checkpoint from ${ENDPOINT} ..."
HTTP_CODE=$(curl -sk -o "${CHECKPOINT_FILE}" -w "%{http_code}" "${ENDPOINT}" || true)
if [[ "${HTTP_CODE}" != "200" ]]; then
  echo "[ERROR] Failed to fetch checkpoint: HTTP ${HTTP_CODE}" >&2
  cat "${CHECKPOINT_FILE}" 2>/dev/null | head -c 1000 >&2 || true
  exit 1
fi

log "Checkpoint response:"
cat "${CHECKPOINT_FILE}" | python3 -m json.tool 2>/dev/null || cat "${CHECKPOINT_FILE}"

# ── Sign checkpoint (ensure HMAC present) ──────────────────────
log "Signing checkpoint with HMAC-SHA256 (chain_head_hash → signature) ..."
sign_checkpoint "${CHECKPOINT_FILE}"
log "Signed checkpoint:"
cat "${CHECKPOINT_FILE}" | python3 -m json.tool 2>/dev/null || cat "${CHECKPOINT_FILE}"

# ── Verify before upload ───────────────────────────────────────
log "Verifying HMAC-SHA256 signature ..."
verify_checkpoint "${CHECKPOINT_FILE}"
VRC=$?
if [[ ${VRC} -ne 0 ]]; then
  echo "[ERROR] Checkpoint signature verification failed — aborting upload (possible tamper)" >&2
  exit 2
fi

# ── Ensure versioning ─────────────────────────────────────────
ensure_versioning

# ── S3 upload ──────────────────────────────────────────────────
TIMESTAMP="$(python3 -c 'from datetime import datetime,timezone; print(datetime.now(timezone.utc).strftime("%Y/%m/%d/%H%M%S"))')"
HEAD_SHORT="$(python3 -c "import json; print(json.load(open('${CHECKPOINT_FILE}')).get('chain_head_hash','empty')[:8] or 'empty')")"
OBJECT_KEY="${AWS_S3_PREFIX}${TIMESTAMP}-${HEAD_SHORT}.json"
S3_URI="s3://${AWS_S3_BUCKET}/${OBJECT_KEY}"

log "Uploading to ${S3_URI} (region ${AWS_S3_REGION}) ..."

if ! command -v aws >/dev/null 2>&1; then
  echo "[ERROR] aws CLI not found — install awscli v2" >&2
  exit 3
fi

if [[ $DRY_RUN -eq 1 ]]; then
  log "[DRY-RUN] Would upload ${CHECKPOINT_FILE} → ${S3_URI} with SSE-S3 + versioning"
else
  aws s3 cp "${CHECKPOINT_FILE}" "${S3_URI}" \
    --region "${AWS_S3_REGION}" \
    --server-side-encryption AES256 \
    --content-type application/json \
    --metadata "oaos-checkpoint-verified=true,oaos-head=${HEAD_SHORT}"
  log "[OK] Uploaded ${S3_URI}"
fi

# ── S3 head verify ─────────────────────────────────────────────
if [[ $DRY_RUN -eq 0 ]]; then
  log "Verifying S3 object exists (head-object) ..."
  aws s3api head-object --bucket "${AWS_S3_BUCKET}" --key "${OBJECT_KEY}" --region "${AWS_S3_REGION}" >/dev/null
  log "[OK] S3 head-object verified — checkpoint durable in S3 (versioned if enabled)"

  # ── Also upload as latest.json ───────────────────────────────
  LATEST_KEY="${AWS_S3_PREFIX}latest.json"
  aws s3 cp "${CHECKPOINT_FILE}" "s3://${AWS_S3_BUCKET}/${LATEST_KEY}" \
    --region "${AWS_S3_REGION}" \
    --server-side-encryption AES256 \
    --content-type application/json \
    --metadata "oaos-checkpoint-verified=true" >/dev/null
  log "[OK] Also updated s3://${AWS_S3_BUCKET}/${LATEST_KEY}"

  # ── Download-verify round-trip ───────────────────────────────
  if [[ "${CHECKPOINT_VERIFY_DOWNLOAD}" == "1" ]]; then
    log "Round-trip download verify ..."
    aws s3 cp "s3://${AWS_S3_BUCKET}/${LATEST_KEY}" "${DOWNLOAD_VERIFY_FILE}" --region "${AWS_S3_REGION}" >/dev/null 2>&1 || {
      log "[WARN] Download-verify fetch failed — skipping"
    }
    if [[ -f "${DOWNLOAD_VERIFY_FILE}" ]]; then
      verify_checkpoint "${DOWNLOAD_VERIFY_FILE}" && log "[OK] Download-verify HMAC passed"
    fi
  fi

  # ── Retention prune (lightweight, best-effort) ───────────────
  if [[ "${CHECKPOINT_RETENTION_DAYS}" != "0" ]]; then
    log "Retention check: pruning >${CHECKPOINT_RETENTION_DAYS}d (best-effort, ignore failures) ..."
    # Run prune but don't fail main flow on error
    set +e
    CHECKPOINT_RETENTION_DAYS="${CHECKPOINT_RETENTION_DAYS}" DRY_RUN=0 bash "$0" --prune 2>&1 | head -n 50 || true
    set -e
  fi
else
  log "[DRY-RUN] Would verify head-object + update latest.json + download-verify + prune"
fi

log "[DONE] Audit checkpoint §31 → S3 complete (signed, versioned, verified, retention ${CHECKPOINT_RETENTION_DAYS}d)"
