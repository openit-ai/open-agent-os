#!/usr/bin/env bash
# audit-checkpoint-to-s3.sh — Audit ledger signed checkpoint → S3 upload (§31)
# Flow: GET /v1/audit/checkpoint → HMAC-SHA256 signature verify → S3 PutObject (SSE-S3) → S3 head verify
# Cron: 0 * * * * /opt/open-agent-os/deploy/scripts/audit-checkpoint-to-s3.sh
# Required env: AUDIT_SIGNING_KEY (HMAC key), AWS_S3_BUCKET, AWS_S3_REGION (optional), SECURITY_URL
# Dependencies: curl, python3, aws cli (v2)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SECURITY_URL="${SECURITY_URL:-http://security:8002}"
ENDPOINT="${SECURITY_URL}/v1/audit/checkpoint"
AWS_S3_BUCKET="${AWS_S3_BUCKET:-}"
AWS_S3_REGION="${AWS_S3_REGION:-ap-northeast-2}"
AWS_S3_PREFIX="${AWS_S3_PREFIX:-audit-checkpoints/}"
AUDIT_SIGNING_KEY="${AUDIT_SIGNING_KEY:-${OAOS_SIGNING_KEY:-}}"
TMPDIR_CHECKPOINT="$(mktemp -d)"
CHECKPOINT_FILE="${TMPDIR_CHECKPOINT}/checkpoint.json"
trap 'rm -rf "${TMPDIR_CHECKPOINT}"' EXIT

usage() {
  echo "Usage: AUDIT_SIGNING_KEY=<key> AWS_S3_BUCKET=<bucket> $0"
  echo "  Env: SECURITY_URL (default http://security:8002)"
  echo "       AWS_S3_REGION (default ap-northeast-2)"
  echo "       AWS_S3_PREFIX (default audit-checkpoints/)"
}

if [[ -z "${AUDIT_SIGNING_KEY}" ]]; then
  echo "[ERROR] AUDIT_SIGNING_KEY (or OAOS_SIGNING_KEY) is required" >&2
  usage >&2
  exit 1
fi
if [[ -z "${AWS_S3_BUCKET}" ]]; then
  echo "[ERROR] AWS_S3_BUCKET is required" >&2
  usage >&2
  exit 1
fi

echo "[INFO] Fetching checkpoint from ${ENDPOINT} ..."
HTTP_CODE=$(curl -sk -o "${CHECKPOINT_FILE}" -w "%{http_code}" "${ENDPOINT}" || true)
if [[ "${HTTP_CODE}" != "200" ]]; then
  echo "[ERROR] Failed to fetch checkpoint: HTTP ${HTTP_CODE}" >&2
  cat "${CHECKPOINT_FILE}" 2>/dev/null | head -c 1000 >&2 || true
  exit 1
fi

echo "[INFO] Checkpoint response:"
cat "${CHECKPOINT_FILE}" | python3 -m json.tool 2>/dev/null || cat "${CHECKPOINT_FILE}"

# ── Signature verification (HMAC-SHA256 over chain_head_hash, §31) ─────
echo "[INFO] Verifying HMAC-SHA256 signature ..."
python3 - "${CHECKPOINT_FILE}" "${AUDIT_SIGNING_KEY}" <<'PY'
import sys, json, hmac, hashlib
path, key = sys.argv[1], sys.argv[2]
with open(path) as f:
    cp = json.load(f)
head = cp.get("chain_head_hash", "")
sig = cp.get("signature", "")
expected = hmac.new(key.encode(), head.encode(), hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, sig):
    print(f"[ERROR] Signature mismatch! expected={expected} got={sig}", file=sys.stderr)
    sys.exit(2)
print(f"[OK] Signature valid — head={head[:16]}... count={cp.get('event_count')}")
PY
VERIFY_EXIT=$?
if [[ ${VERIFY_EXIT} -ne 0 ]]; then
  echo "[ERROR] Checkpoint signature verification failed — aborting upload (possible tamper)" >&2
  exit 2
fi

# ── S3 upload ─────────────────────────────────────────────────────
TIMESTAMP="$(python3 -c 'from datetime import datetime,timezone; print(datetime.now(timezone.utc).strftime("%Y/%m/%d/%H%M%S"))')"
# Derive object key: <prefix><YYYY>/<MM>/<DD>/<HHMMSS>-<head8>.json
HEAD_SHORT="$(python3 -c "import json; print(json.load(open('${CHECKPOINT_FILE}')).get('chain_head_hash','empty')[:8] or 'empty')")"
OBJECT_KEY="${AWS_S3_PREFIX}${TIMESTAMP}-${HEAD_SHORT}.json"
S3_URI="s3://${AWS_S3_BUCKET}/${OBJECT_KEY}"

echo "[INFO] Uploading to ${S3_URI} (region ${AWS_S3_REGION}) ..."

if ! command -v aws >/dev/null 2>&1; then
  echo "[ERROR] aws CLI not found — install awscli v2: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html" >&2
  exit 3
fi

aws s3 cp "${CHECKPOINT_FILE}" "${S3_URI}" \
  --region "${AWS_S3_REGION}" \
  --server-side-encryption AES256 \
  --content-type application/json \
  --metadata "oaos-checkpoint-verified=true"

echo "[OK] Uploaded ${S3_URI}"

# ── S3 verification (HEAD) ───────────────────────────────────────
echo "[INFO] Verifying S3 object exists ..."
aws s3api head-object --bucket "${AWS_S3_BUCKET}" --key "${OBJECT_KEY}" --region "${AWS_S3_REGION}" >/dev/null
echo "[OK] S3 head-object verified — checkpoint durable in S3"

# ── Optional: also upload as 'latest.json' for quick retrieval ──
LATEST_KEY="${AWS_S3_PREFIX}latest.json"
aws s3 cp "${CHECKPOINT_FILE}" "s3://${AWS_S3_BUCKET}/${LATEST_KEY}" \
  --region "${AWS_S3_REGION}" \
  --server-side-encryption AES256 \
  --content-type application/json >/dev/null
echo "[OK] Also updated s3://${AWS_S3_BUCKET}/${LATEST_KEY}"
echo "[DONE] Audit checkpoint §31 → S3 complete"
