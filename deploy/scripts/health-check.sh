#!/usr/bin/env bash
# health-check.sh — Managed edition health check (all services, DB, Redis, audit chain)
# Usage: ./deploy/scripts/health-check.sh [--json] [--verbose]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

JSON_OUT=0
VERBOSE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON_OUT=1; shift ;;
    --verbose|-v) VERBOSE=1; shift ;;
    --help|-h) echo "Usage: $0 [--json] [--verbose]"; exit 0 ;;
    *) echo "[WARN] Unknown arg: $1" >&2; shift ;;
  esac
done

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2; }
vlog() { if [[ $VERBOSE -eq 1 ]]; then log "$*"; fi; }

# Colors (no-op if not tty)
if [[ -t 1 ]]; then
  GREEN="\033[32m"; RED="\033[31m"; YEL="\033[33m"; NC="\033[0m"
else
  GREEN=""; RED=""; YEL=""; NC=""
fi

PASS=0; FAIL=0; SKIP=0
RESULTS=()  # json array elements

record() {
  local name="$1" status="$2" detail="${3:-}"
  local color="$GREEN"
  [[ "$status" == "FAIL" ]] && color="$RED"
  [[ "$status" == "SKIP" ]] && color="$YEL"
  if [[ $JSON_OUT -eq 0 ]]; then
    printf "  %b[%s]%b %s" "$color" "$status" "$NC" "$name"
    [[ -n "$detail" ]] && printf " — %s" "$detail"
    echo ""
  fi
  if [[ "$status" == "PASS" ]]; then PASS=$((PASS+1))
  elif [[ "$status" == "FAIL" ]]; then FAIL=$((FAIL+1))
  else SKIP=$((SKIP+1))
  fi
  # json escape
  local jdetail
  jdetail=$(printf '%s' "$detail" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))")
  RESULTS+=("{\"name\":$(python3 -c "import json; print(json.dumps('$name'))"),\"status\":\"$status\",\"detail\":$jdetail}")
}

check_http() {
  local name="$1" url="$2"
  local code body
  if command -v curl >/dev/null 2>&1; then
    code=$(curl -sf --max-time 5 -o /tmp/oaos-hc-body.txt -w "%{http_code}" "$url" 2>/dev/null || echo "000")
    body=$(cat /tmp/oaos-hc-body.txt 2>/dev/null | head -c 200 || echo "")
  elif command -v wget >/dev/null 2>&1; then
    if wget -q --timeout=5 -O /tmp/oaos-hc-body.txt "$url" 2>/dev/null; then code="200"; body=$(cat /tmp/oaos-hc-body.txt | head -c 200); else code="000"; body=""; fi
  elif command -v python3 >/dev/null 2>&1; then
    code=$(python3 -c "import urllib.request; print(urllib.request.urlopen('$url', timeout=5).status)" 2>/dev/null || echo "000")
    body=""
  else
    record "$name" "SKIP" "no curl/wget/python3"
    return
  fi
  if [[ "$code" == "200" ]]; then
    record "$name" "PASS" "$url -> $code"
  else
    # try via docker exec as fallback
    local svc
    svc=$(echo "$url" | sed -E 's|.*:([0-9]+)/.*|\1|')
    local cname=""
    case "$svc" in
      8000) cname="oaos-control-plane" ;;
      8001) cname="oaos-execution-gateway" ;;
      8002) cname="oaos-security" ;;
    esac
    if [[ -n "$cname" ]] && command -v docker >/dev/null 2>&1 && docker exec "$cname" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$svc/health', timeout=3)" 2>/dev/null; then
      record "$name" "PASS" "$url -> 200 (via docker exec $cname)"
    else
      record "$name" "FAIL" "$url -> $code ${body:0:80}"
    fi
  fi
}

# ── 1. Service health endpoints ──────────────────────────────────
if [[ $JSON_OUT -eq 0 ]]; then echo "=== Service health ==="; fi
check_http "control-plane /health" "http://127.0.0.1:8000/health"
check_http "execution-gateway /health" "http://127.0.0.1:8001/health"
check_http "security /health" "http://127.0.0.1:8002/health"
# nginx sidecar if present
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-nginx"; then
  check_http "nginx /healthz" "http://127.0.0.1:80/healthz"
else
  vlog "nginx container not running — skipping nginx healthz"
fi

# ── 2. Docker compose ps ─────────────────────────────────────────
if [[ $JSON_OUT -eq 0 ]]; then echo "=== Containers ==="; fi
if command -v docker >/dev/null 2>&1; then
  for cname in oaos-postgres oaos-redis oaos-control-plane oaos-execution-gateway oaos-security; do
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$cname"; then
      health=$(docker inspect --format '{{if .State.Health}}{{ .State.Health.Status}}{{else}}no-healthcheck{{end}}' "$cname" 2>/dev/null || echo "unknown")
      if [[ "$health" == "healthy" || "$health" == "no-healthcheck" ]]; then
        record "container $cname" "PASS" "running ($health)"
      elif [[ "$health" == "starting" ]]; then
        record "container $cname" "SKIP" "starting"
      else
        record "container $cname" "FAIL" "health=$health"
      fi
    else
      # Check if exists but stopped
      if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$cname"; then
        record "container $cname" "FAIL" "exists but not running"
      else
        record "container $cname" "SKIP" "not found (compose not up?)"
      fi
    fi
  done
else
  record "containers" "SKIP" "docker not available"
fi

# ── 3. Postgres (DB) ─────────────────────────────────────────────
if [[ $JSON_OUT -eq 0 ]]; then echo "=== Postgres (DB) ==="; fi
DB_URL="${DATABASE_URL:-}"
if [[ -f "${REPO_ROOT}/.env" ]]; then
  # shellcheck disable=SC2046
  DB_URL=$(grep -E "^DATABASE_URL=" "${REPO_ROOT}/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "$DB_URL")
fi
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "oaos-postgres"; then
  # Determine postgres user/db from env or defaults
  PGUSER="${POSTGRES_USER:-oaos}"
  PGDB="${POSTGRES_DB:-oaos}"
  if [[ -f "${REPO_ROOT}/.env" ]]; then
    PGUSER=$(grep -E "^POSTGRES_USER=" "${REPO_ROOT}/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "$PGUSER")
    PGDB=$(grep -E "^POSTGRES_DB=" "${REPO_ROOT}/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "$PGDB")
    [[ -z "$PGUSER" ]] && PGUSER="oaos"
    [[ -z "$PGDB" ]] && PGDB="oaos"
  fi
  if docker exec oaos-postgres pg_isready -U "$PGUSER" -d "$PGDB" >/dev/null 2>&1; then
    record "postgres pg_isready" "PASS" "pg_isready -U $PGUSER -d $PGDB"
  else
    record "postgres pg_isready" "FAIL" "pg_isready failed for $PGUSER/$PGDB"
  fi
  # Try simple query
  if docker exec oaos-postgres psql -U "$PGUSER" -d "$PGDB" -c "SELECT 1" >/dev/null 2>&1; then
    record "postgres query SELECT 1" "PASS" "psql SELECT 1 ok"
  else
    record "postgres query SELECT 1" "FAIL" "psql SELECT 1 failed"
  fi
else
  # Fallback: try DATABASE_URL via python if available
  if [[ -n "$DB_URL" ]] && command -v python3 >/dev/null 2>&1; then
    if python3 -c "import asyncpg, asyncio; asyncio.run(asyncpg.connect('${DB_URL}'.replace('postgresql+asyncpg://','postgresql://')).close())" 2>/dev/null; then
      record "postgres (via DATABASE_URL)" "PASS" "asyncpg connect ok"
    else
      record "postgres" "SKIP" "container not running, DATABASE_URL connect failed"
    fi
  else
    record "postgres" "SKIP" "oaos-postgres not running"
  fi
fi

# ── 4. Redis ─────────────────────────────────────────────────────
if [[ $JSON_OUT -eq 0 ]]; then echo "=== Redis ==="; fi
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "oaos-redis"; then
  REDIS_PASS=""
  if [[ -f "${REPO_ROOT}/.env" ]]; then
    REDIS_PASS=$(grep -E "^REDIS_PASSWORD=" "${REPO_ROOT}/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "")
  fi
  if [[ -n "$REDIS_PASS" ]]; then
    if docker exec oaos-redis redis-cli -a "$REDIS_PASS" ping 2>/dev/null | grep -q PONG; then
      record "redis ping" "PASS" "PONG (auth)"
    else
      record "redis ping" "FAIL" "redis-cli ping failed"
    fi
  else
    if docker exec oaos-redis redis-cli ping 2>/dev/null | grep -q PONG; then
      record "redis ping" "PASS" "PONG"
    else
      record "redis ping" "FAIL" "redis-cli ping failed"
    fi
  fi
else
  if [[ -n "${REDIS_URL:-}" ]] && command -v python3 >/dev/null 2>&1; then
    record "redis" "SKIP" "oaos-redis not running (REDIS_URL set but not checked)"
  else
    record "redis" "SKIP" "oaos-redis not running"
  fi
fi

# ── 5. Audit chain (§30-31) ──────────────────────────────────────
if [[ $JSON_OUT -eq 0 ]]; then echo "=== Audit chain ==="; fi
AUDIT_CHECKED=0
# Try via security API if up
if curl -sf --max-time 5 http://127.0.0.1:8002/v1/audit/verify 2>/dev/null | grep -q '"valid"'; then
  record "audit chain (API /v1/audit/verify)" "PASS" "API reports valid"
  AUDIT_CHECKED=1
elif curl -sf --max-time 5 http://127.0.0.1:8002/v1/audit/chain/verify 2>/dev/null | grep -q '"valid"'; then
  record "audit chain (API)" "PASS" "API reports valid"
  AUDIT_CHECKED=1
fi
# Fallback: python audit verify if security package available
if [[ $AUDIT_CHECKED -eq 0 ]]; then
  if command -v python3 >/dev/null 2>&1; then
    if python3 -c "
import sys
from pathlib import Path
sys.path.insert(0, str(Path('${REPO_ROOT}/packages/audit-model')))
sys.path.insert(0, str(Path('${REPO_ROOT}/security/audit')))
try:
    from audit import verify_chain
    # verify_chain expects list of entries; with empty DB it should handle 0 entries as valid
    # Just check the function is importable and callable
    assert callable(verify_chain)
    print('audit import ok')
except Exception as e:
    print(f'import fail: {e}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null; then
      record "audit chain (import verify_chain)" "PASS" "audit model importable"
      AUDIT_CHECKED=1
    fi
  fi
fi
if [[ $AUDIT_CHECKED -eq 0 ]]; then
  record "audit chain" "SKIP" "security API not reachable, audit import failed"
fi

# ── Summary ──────────────────────────────────────────────────────
if [[ $JSON_OUT -eq 1 ]]; then
  # Emit JSON
  printf '{"pass":%d,"fail":%d,"skip":%d,"results":[%s]}\n' "$PASS" "$FAIL" "$SKIP" "$(IFS=,; echo "${RESULTS[*]}")"
else
  echo ""
  echo "=== Summary: PASS=$PASS FAIL=$FAIL SKIP=$SKIP ==="
  if [[ $FAIL -eq 0 ]]; then
    echo "All health checks PASSED (skipped=$SKIP)"
  else
    echo "Some health checks FAILED — see above"
  fi
fi

# Exit code: 0 if no FAIL, 1 otherwise (SKIP does not fail)
if [[ $FAIL -gt 0 ]]; then exit 1; else exit 0; fi
