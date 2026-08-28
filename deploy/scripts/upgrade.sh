#!/usr/bin/env bash
# upgrade.sh — Business edition rolling upgrade (zero-downtime via compose)
# Steps: 1) record current IMAGE_TAG, 2) pull images, 3) alembic upgrade, 4) rolling restart per service, 5) health check, 6) rollback on fail
# Usage: ./deploy/scripts/upgrade.sh [--dry-run] [--compose-file FILE] [--tag TAG] [--skip-migrate]
# Env: IMAGE_TAG, COMPOSE_FILE, HEALTH_CHECK_RETRIES (30), HEALTH_CHECK_INTERVAL (5), ROLLBACK_ON_FAIL (1)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DRY_RUN=0
COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/deploy/docker-compose.prod.yml}"
TARGET_TAG="${IMAGE_TAG:-}"
SKIP_MIGRATE=0
HEALTH_RETRIES="${HEALTH_CHECK_RETRIES:-30}"
HEALTH_INTERVAL="${HEALTH_CHECK_INTERVAL:-5}"
ROLLBACK_ON_FAIL="${ROLLBACK_ON_FAIL:-1}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --compose-file) COMPOSE_FILE="$2"; shift 2 ;;
    --tag) TARGET_TAG="$2"; shift 2 ;;
    --skip-migrate) SKIP_MIGRATE=1; shift ;;
    --help|-h)
      echo "Usage: $0 [--dry-run] [--compose-file FILE] [--tag TAG] [--skip-migrate]"
      echo "  Env: IMAGE_TAG, COMPOSE_FILE, HEALTH_CHECK_RETRIES, ROLLBACK_ON_FAIL"
      exit 0
      ;;
    *) echo "[WARN] Unknown arg: $1" >&2; shift ;;
  esac
done

if [[ "${OAOS_UPGRADE_DRY_RUN:-}" == "1" ]]; then DRY_RUN=1; fi
if [[ -n "${TARGET_TAG}" ]]; then export IMAGE_TAG="${TARGET_TAG}"; fi

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2; }
dry_prefix() { if [[ $DRY_RUN -eq 1 ]]; then echo "[DRY-RUN] "; fi; }

# Track for rollback
PREVIOUS_TAG=""
UPGRADE_FAILED=0

compose() {
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would run: docker compose -f ${COMPOSE_FILE} $*"
    return 0
  fi
  docker compose -f "${COMPOSE_FILE}" "$@"
}

health_check() {
  local attempt=0
  local services=("control-plane:8000" "execution-gateway:8001" "security:8002")
  # In prod via nginx, also check nginx healthz; but direct service health is primary
  log "Health check: ${HEALTH_RETRIES} retries x ${HEALTH_INTERVAL}s"
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would health-check ${services[*]}"
    return 0
  fi
  # Allow forced failure for testing rollback
  if [[ "${OAOS_UPGRADE_FORCE_FAIL:-}" == "1" ]]; then
    log "[TEST] OAOS_UPGRADE_FORCE_FAIL=1 — simulating health check failure"
    return 1
  fi
  while [[ $attempt -lt $HEALTH_RETRIES ]]; do
    local all_ok=1
    for svc in "${services[@]}"; do
      local name="${svc%%:*}"
      local port="${svc##*:}"
      local url="http://127.0.0.1:${port}/health"
      # Try via container exec first, then host
      local ok=0
      if docker exec "oaos-${name}" python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${port}/health', timeout=3).status==200 else 1)" 2>/dev/null; then
        ok=1
      elif curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
        ok=1
      elif docker inspect --format '{{.State.Health.Status}}' "oaos-${name}" 2>/dev/null | grep -q "healthy"; then
        ok=1
      fi
      if [[ $ok -eq 0 ]]; then
        all_ok=0
        log "[WAIT] ${name} not healthy (attempt $((attempt+1))/${HEALTH_RETRIES})"
        break
      fi
    done
    if [[ $all_ok -eq 1 ]]; then
      log "[OK] All services healthy"
      return 0
    fi
    attempt=$((attempt+1))
    sleep "${HEALTH_INTERVAL}"
  done
  log "[ERROR] Health check failed after ${HEALTH_RETRIES} attempts"
  return 1
}

do_migrate() {
  if [[ $SKIP_MIGRATE -eq 1 ]]; then
    log "[INFO] Skipping alembic upgrade (--skip-migrate)"
    return 0
  fi
  log "$(dry_prefix)Alembic upgrade head"
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would run: alembic upgrade head (or docker exec control-plane alembic upgrade head)"
    return 0
  fi
  # Prefer running inside control-plane container if available
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-control-plane"; then
    if docker exec oaos-control-plane alembic upgrade head 2>&1 | log; then
      log "[OK] alembic upgrade head (via container)"
      return 0
    else
      log "[WARN] container alembic failed — trying host alembic"
    fi
  fi
  if command -v alembic >/dev/null 2>&1; then
    alembic upgrade head 2>&1 | log || { log "[ERROR] alembic upgrade head failed"; return 1; }
    log "[OK] alembic upgrade head (via host)"
  else
    log "[WARN] alembic not found — skipping migration (ensure image handles it on startup)"
  fi
}

rollback() {
  if [[ "${ROLLBACK_ON_FAIL}" != "1" ]]; then
    log "[WARN] Rollback disabled (ROLLBACK_ON_FAIL!=1) — leaving failed state"
    return 1
  fi
  log "[ROLLBACK] Rolling back to previous tag: ${PREVIOUS_TAG:-unknown}"
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would rollback: IMAGE_TAG=${PREVIOUS_TAG} docker compose up -d"
    return 0
  fi
  if [[ -n "${PREVIOUS_TAG}" ]]; then
    export IMAGE_TAG="${PREVIOUS_TAG}"
  else
    log "[WARN] No previous tag recorded — attempting compose restart with current config"
  fi
  compose up -d 2>&1 | log || log "[ERROR] Rollback compose up failed"
  # Revert alembic if needed (downgrade by one) — best effort
  if [[ $SKIP_MIGRATE -eq 0 ]] && [[ -n "${PREVIOUS_TAG}" ]]; then
    log "[INFO] Rollback: alembic downgrade -1 (best effort)"
    if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-control-plane"; then
      docker exec oaos-control-plane alembic downgrade -1 2>&1 | log || log "[WARN] alembic downgrade failed — manual intervention may be needed"
    elif command -v alembic >/dev/null 2>&1; then
      alembic downgrade -1 2>&1 | log || log "[WARN] alembic downgrade failed"
    fi
  fi
  sleep 5
  if health_check; then
    log "[OK] Rollback health check passed"
  else
    log "[ERROR] Rollback health check still failing — manual intervention required"
  fi
}

main() {
  log "=== OAOS Upgrade start dry_run=${DRY_RUN} compose=${COMPOSE_FILE} target_tag=${TARGET_TAG:-latest} ==="

  if [[ ! -f "${COMPOSE_FILE}" ]]; then
    log "[ERROR] Compose file not found: ${COMPOSE_FILE}"; exit 1; fi

  # Record previous tag (from env or image inspect)
  PREVIOUS_TAG="${IMAGE_TAG:-}"
  if [[ -z "${PREVIOUS_TAG}" ]]; then
    PREVIOUS_TAG=$(docker inspect --format '{{.Config.Image}}' oaos-control-plane 2>/dev/null | rev | cut -d: -f1 | rev || echo "latest")
  fi
  log "[INFO] Previous IMAGE_TAG=${PREVIOUS_TAG}"

  # Trap rollback on failure (unless dry-run)
  if [[ $DRY_RUN -eq 0 ]] && [[ "${ROLLBACK_ON_FAIL}" == "1" ]]; then
    trap 'if [[ $UPGRADE_FAILED -eq 1 ]]; then rollback; fi' EXIT
  fi

  # 1. Pull images
  log "$(dry_prefix)Pulling images (docker compose pull)"
  if ! compose pull 2>&1 | log; then
    log "[ERROR] docker compose pull failed"
    UPGRADE_FAILED=1
    if [[ $DRY_RUN -eq 1 ]]; then
      log "[DRY-RUN] Would have failed pull — continuing dry-run"
    else
      exit 1
    fi
  fi

  # 2. Alembic upgrade BEFORE restarting (so new code matches schema) — or after pull depending on strategy
  # We run before restart to minimize downtime; if migration fails, we abort before restart
  if ! do_migrate; then
    log "[ERROR] Migration failed — aborting upgrade"
    UPGRADE_FAILED=1
    exit 1
  fi

  # 3. Rolling restart: update one service at a time for zero-downtime
  # In compose without swarm, we do: up -d --no-deps <service> sequentially
  local services_order=("security" "execution-gateway" "control-plane" "nginx")
  for svc in "${services_order[@]}"; do
    log "$(dry_prefix)Rolling update service: ${svc}"
    if ! compose up -d --no-deps "${svc}" 2>&1 | log; then
      log "[ERROR] Failed to update ${svc}"
      UPGRADE_FAILED=1
      exit 1
    fi
    # Brief health check after each service (except maybe nginx)
    if [[ $DRY_RUN -eq 0 ]]; then
      sleep 3
    fi
  done

  # Final: ensure all services are up (compose up -d reconciles dependencies)
  log "$(dry_prefix)Final compose up -d (reconcile)"
  compose up -d 2>&1 | log || { UPGRADE_FAILED=1; exit 1; }

  # 4. Health check
  if ! health_check; then
    log "[ERROR] Post-upgrade health check failed — triggering rollback"
    UPGRADE_FAILED=1
    if [[ $DRY_RUN -eq 1 ]]; then
      log "[DRY-RUN] Would rollback now"
    else
      rollback
      exit 1
    fi
  fi

  # Success: disable rollback trap
  UPGRADE_FAILED=0
  trap - EXIT

  log "=== OAOS Upgrade complete (tag=${TARGET_TAG:-${IMAGE_TAG:-latest}}) ==="
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Upgrade dry-run finished — no changes made"
  fi
}

main "$@"
