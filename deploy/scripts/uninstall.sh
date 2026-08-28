#!/usr/bin/env bash
# uninstall.sh — Managed edition uninstall / teardown
# Usage: sudo ./deploy/scripts/uninstall.sh [--dry-run] [--keep-data] [--keep-user] [--yes]
# Default: stops containers, removes volumes (unless --keep-data), removes nftables, optionally removes hermes user.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DRY_RUN=0
KEEP_DATA=0
KEEP_USER=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --keep-data) KEEP_DATA=1; shift ;;
    --keep-user) KEEP_USER=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --help|-h)
      echo "Usage: $0 [--dry-run] [--keep-data] [--keep-user] [--yes]"
      echo "  --dry-run    Print actions without executing"
      echo "  --keep-data  Preserve Docker volumes (pgdata, redisdata)"
      echo "  --keep-user  Preserve hermes OS user"
      echo "  --yes        Skip confirmation prompt"
      exit 0
      ;;
    *) echo "[WARN] Unknown arg: $1" >&2; shift ;;
  esac
done
if [[ "${OAOS_DRY_RUN:-}" == "1" ]]; then DRY_RUN=1; fi

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2; }

run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would run: $*"
  else
    "$@"
  fi
}

if [[ $EUID -ne 0 && $DRY_RUN -eq 0 ]]; then
  echo "[ERROR] Run as root: sudo $0" >&2
  exit 1
fi

if [[ $ASSUME_YES -eq 0 && $DRY_RUN -eq 0 ]]; then
  echo "This will stop and remove Open Agent OS containers and related resources."
  if [[ $KEEP_DATA -eq 0 ]]; then echo "  WARNING: Docker volumes (DB/Redis data) will be REMOVED."; fi
  if [[ $KEEP_USER -eq 0 ]]; then echo "  hermes OS user will be removed."; fi
  read -rp "Continue? [y/N] " _ans || true
  if [[ "${_ans:-}" != "y" && "${_ans:-}" != "Y" ]]; then echo "Aborted."; exit 0; fi
fi

if [[ $DRY_RUN -eq 1 ]]; then
  log "[DRY-RUN] Uninstall preview — no changes will be made"
fi

# ── 1. docker compose down ───────────────────────────────────────
log "[INFO] Step 1/4: docker compose down"
COMPOSE_FILE="${REPO_ROOT}/deploy/docker-compose.prod.yml"
if [[ ! -f "$COMPOSE_FILE" ]]; then COMPOSE_FILE="${REPO_ROOT}/deploy/docker-compose.dev.yml"; fi
if [[ -f "$COMPOSE_FILE" ]]; then
  COMPOSE="docker compose"
  if ! docker compose version >/dev/null 2>&1; then
    if command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"; else COMPOSE=""; fi
  fi
  if [[ -n "$COMPOSE" ]]; then
    if [[ $KEEP_DATA -eq 1 ]]; then
      run $COMPOSE -f "$COMPOSE_FILE" down 2>&1 || log "[WARN] compose down failed (maybe not running)"
    else
      run $COMPOSE -f "$COMPOSE_FILE" down -v 2>&1 || log "[WARN] compose down -v failed"
    fi
  else
    log "[WARN] docker compose not found — skipping container teardown"
  fi
else
  log "[WARN] No compose file found — skipping compose down"
  # Fallback: try to stop known containers
  for c in oaos-nginx oaos-control-plane oaos-execution-gateway oaos-security oaos-postgres oaos-redis; do
    if command -v docker >/dev/null 2>&1 && docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
      run docker rm -f "$c" 2>&1 || true
    fi
  done
fi

# ── 2. nftables cleanup ──────────────────────────────────────────
log "[INFO] Step 2/4: nftables cleanup"
if [[ -f /etc/nftables/hermes-egress.nft ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would remove /etc/nftables/hermes-egress.nft and flush hermes_egress table"
  else
    rm -f /etc/nftables/hermes-egress.nft
    if command -v nft >/dev/null 2>&1; then
      nft delete table inet hermes_egress 2>/dev/null || true
      log "[INFO] hermes_egress nft table flushed (if existed)"
    fi
  fi
else
  log "[INFO] No /etc/nftables/hermes-egress.nft — skipping"
fi
# Also try generic flush if table exists regardless of file
if [[ $DRY_RUN -eq 0 && -x "$(command -v nft 2>/dev/null || echo /nope)" ]]; then
  nft delete table inet hermes_egress 2>/dev/null || true
fi
if [[ $DRY_RUN -eq 1 ]]; then
  log "[DRY-RUN] Would run: nft delete table inet hermes_egress (if exists)"
fi

# ── 3. hermes OS user ────────────────────────────────────────────
log "[INFO] Step 3/4: hermes OS user"
if [[ $KEEP_USER -eq 1 ]]; then
  log "[INFO] --keep-user: preserving hermes user"
else
  if id hermes >/dev/null 2>&1; then
    if [[ $DRY_RUN -eq 1 ]]; then
      log "[DRY-RUN] Would remove hermes user (userdel -r hermes, remove /etc/sudoers.d/99-hermes-deny)"
    else
      rm -f /etc/sudoers.d/99-hermes-deny
      # Kill processes owned by hermes
      pkill -u hermes 2>/dev/null || true
      sleep 1
      userdel -r hermes 2>/dev/null || userdel hermes 2>/dev/null || {
        log "[WARN] userdel failed — try: sudo userdel -r hermes"
      }
      log "[INFO] hermes user removed"
    fi
  else
    log "[INFO] hermes user not found — skipping"
  fi
fi

# ── 4. .env handling ─────────────────────────────────────────────
log "[INFO] Step 4/4: .env"
if [[ -f "${REPO_ROOT}/.env" ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would keep ${REPO_ROOT}/.env (remove manually if needed: rm ${REPO_ROOT}/.env)"
  else
    log "[INFO] Keeping ${REPO_ROOT}/.env (remove manually if needed)"
  fi
else
  log "[INFO] No .env to clean"
fi

log "[INFO] Uninstall done"
if [[ $DRY_RUN -eq 1 ]]; then log "[DRY-RUN] No changes were made"; fi
echo "  To fully purge data: docker volume prune -f  (if --keep-data was not used, volumes already removed)"
echo "  To remove .env: rm ${REPO_ROOT}/.env"
