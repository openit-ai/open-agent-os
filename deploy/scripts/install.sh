#!/usr/bin/env bash
# install.sh — Managed edition one-click VPS installer
# Business + VPS auto-install + remote monitoring + upgrade automation
# Usage: sudo ./deploy/scripts/install.sh [--domain example.com] [--email admin@example.com] [--non-interactive] [--dry-run]
# Env: OAOS_DOMAIN, OAOS_EMAIL, DOMAIN, EMAIL, OAOS_DRY_RUN
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# ── args ──────────────────────────────────────────────────────────
DOMAIN="${OAOS_DOMAIN:-${DOMAIN:-}}"
EMAIL="${OAOS_EMAIL:-${EMAIL:-}}"
NON_INTERACTIVE=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h)
      echo "Usage: $0 [--domain DOMAIN] [--email EMAIL] [--non-interactive] [--dry-run]"
      echo "  --domain DOMAIN       FQDN for TLS/ingress (e.g. oaos.example.com)"
      echo "  --email EMAIL         ACME email for Let's Encrypt (requires --domain)"
      echo "  --non-interactive     Fail instead of prompting when values missing"
      echo "  --dry-run             Print actions without executing"
      echo "  Env: OAOS_DOMAIN / DOMAIN, OAOS_EMAIL / EMAIL, OAOS_DRY_RUN=1"
      exit 0
      ;;
    *) echo "[WARN] Unknown arg: $1" >&2; shift ;;
  esac
done
if [[ "${OAOS_DRY_RUN:-}" == "1" ]]; then DRY_RUN=1; fi

log()  { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2; }
info() { log "[INFO] $*"; }
warn() { log "[WARN] $*"; }
dry()  { if [[ $DRY_RUN -eq 1 ]]; then echo "[DRY-RUN] $*"; fi; }

run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would run: $*"
  else
    "$@"
  fi
}

require_not_dry() {
  # helper: execute only when not dry-run; else log
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would run: $*"
    return 0
  fi
  "$@"
}

# ── 0. Privilege check (warn in dry-run) ─────────────────────────
if [[ $EUID -ne 0 && $DRY_RUN -eq 0 ]]; then
  echo "[ERROR] Run as root: sudo $0 [--domain ...] [--email ...]" >&2
  exit 1
fi
if [[ $EUID -ne 0 && $DRY_RUN -eq 1 ]]; then
  info "DRY-RUN: root check skipped (not root)"
fi

# ── 1. Check dependencies ────────────────────────────────────────
info "Step 1/8: Checking dependencies (docker, compose, nft, openssl, python)"
check_dep() {
  local bin="$1" label="${2:-$1}"
  if command -v "$bin" >/dev/null 2>&1; then
    info "  $label: $(command -v "$bin") ($($bin --version 2>&1 | head -n1))"
  else
    if [[ $DRY_RUN -eq 1 ]]; then
      warn "  $label: not found (would be required in real install)"
    else
      echo "[ERROR] Missing dependency: $bin ($label)" >&2
      exit 1
    fi
  fi
}
if [[ $DRY_RUN -eq 1 ]]; then
  for b in docker nft openssl python3; do
    if command -v "$b" >/dev/null 2>&1; then info "  $b: found"; else warn "  $b: not found (dry-run ok)"; fi
  done
  # compose can be docker compose or docker-compose
  if docker compose version >/dev/null 2>&1; then info "  docker compose: found"
  elif command -v docker-compose >/dev/null 2>&1; then info "  docker-compose: found"
  else warn "  docker compose: not found (dry-run ok)"; fi
else
  check_dep docker
  if docker compose version >/dev/null 2>&1; then
    info "  docker compose plugin: $(docker compose version 2>&1 | head -n1)"
  elif command -v docker-compose >/dev/null 2>&1; then
    info "  docker-compose: $(docker-compose --version 2>&1 | head -n1)"
  else
    echo "[ERROR] docker compose not found (need 'docker compose' plugin or docker-compose)" >&2
    exit 1
  fi
  check_dep nft nftables
  check_dep openssl
  check_dep python3
fi

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  else
    echo "docker-compose"
  fi
}
COMPOSE="$(compose_cmd 2>/dev/null || echo "docker compose")"
info "  compose command: $COMPOSE"

# ── 2. Create hermes OS user ─────────────────────────────────────
info "Step 2/8: Ensure hermes OS user (§16A.4)"
if [[ -x "${SCRIPT_DIR}/create-hermes-user.sh" ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would run: ${SCRIPT_DIR}/create-hermes-user.sh"
  else
    bash "${SCRIPT_DIR}/create-hermes-user.sh"
  fi
else
  warn "create-hermes-user.sh not found at ${SCRIPT_DIR}/create-hermes-user.sh — creating minimal hermes user"
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would create hermes user (useradd --system hermes, home /home/hermes 0750, sudo deny, passwd -l)"
  else
    if ! id hermes >/dev/null 2>&1; then
      groupadd --system hermes 2>/dev/null || true
      useradd --system --gid hermes --home-dir /home/hermes --create-home --shell /bin/bash --comment "Hermes Runtime (§16A.4)" hermes
    fi
    mkdir -p /home/hermes && chown hermes:hermes /home/hermes && chmod 0750 /home/hermes
    cat > /etc/sudoers.d/99-hermes-deny <<'EOS'
hermes ALL=(ALL) !ALL
Defaults:hermes !authenticate
EOS
    chmod 0440 /etc/sudoers.d/99-hermes-deny
    passwd -l hermes >/dev/null 2>&1 || true
    info "hermes user ready"
  fi
fi

# Handle --domain/--email prompting
if [[ -z "${DOMAIN}" && $NON_INTERACTIVE -eq 0 && $DRY_RUN -eq 0 ]]; then
  read -rp "Domain (FQDN, empty to skip TLS) [${DOMAIN:-none}]: " _d || true
  DOMAIN="${_d:-$DOMAIN}"
fi
if [[ -n "${DOMAIN}" && -z "${EMAIL}" && $NON_INTERACTIVE -eq 0 && $DRY_RUN -eq 0 ]]; then
  read -rp "ACME email for Let's Encrypt [${EMAIL:-none}]: " _e || true
  EMAIL="${_e:-$EMAIL}"
fi
if [[ $NON_INTERACTIVE -eq 1 && -n "${DOMAIN}" && -z "${EMAIL}" ]]; then
  warn "--domain given without --email and --non-interactive: TLS auto-issue will be skipped (provide --email to enable)"
fi

# ── 3. Apply nftables ────────────────────────────────────────────
info "Step 3/8: Apply nftables (hermes-egress.nft §16A.6)"
NFT_SRC="${REPO_ROOT}/deploy/firewall/hermes-egress.nft"
NFT_DST="/etc/nftables/hermes-egress.nft"
if [[ -f "$NFT_SRC" ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would install $NFT_SRC -> $NFT_DST and run: nft -f $NFT_DST (or nft --check -f $NFT_SRC)"
    if command -v nft >/dev/null 2>&1; then
      nft --check -f "$NFT_SRC" 2>&1 && info "  nft syntax check: OK" || warn "  nft syntax check: FAIL (dry-run warn only)"
    fi
  else
    mkdir -p /etc/nftables
    cp "$NFT_SRC" "$NFT_DST"
    info "  Installed $NFT_DST"
    if command -v nft >/dev/null 2>&1; then
      if nft --check -f "$NFT_DST" 2>&1; then
        info "  nft syntax: OK"
      else
        warn "  nft syntax check failed — not loading"
      fi
      # Try to load (non-fatal if fails)
      nft -f "$NFT_DST" 2>&1 && info "  nft ruleset loaded" || warn "  nft load failed — check $NFT_DST manually"
    else
      warn "  nft not installed — ruleset staged at $NFT_DST (install nftables to enforce)"
    fi
  fi
else
  warn "hermes-egress.nft not found at $NFT_SRC — skipping nftables"
fi

# ── 4. Generate .env from template ────────────────────────────────
info "Step 4/8: Generate .env"
ENV_FILE="${REPO_ROOT}/.env"
ENV_EXAMPLE="${REPO_ROOT}/.env.example"
if [[ -f "$ENV_FILE" && $DRY_RUN -eq 0 ]]; then
  info "  .env already exists at $ENV_FILE — not overwriting (remove to regenerate)"
else
  if [[ ! -f "$ENV_EXAMPLE" ]]; then
    warn ".env.example not found — generating minimal .env"
  fi
  # Generate secrets
  gen_secret() {
    if command -v openssl >/dev/null 2>&1; then
      openssl rand -base64 32 | tr -d '\n'
    else
      python3 -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
    fi
  }
  gen_hex() {
    if command -v openssl >/dev/null 2>&1; then
      openssl rand -hex 32
    else
      python3 -c "import secrets; print(secrets.token_hex(32))"
    fi
  }
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would generate $ENV_FILE from $ENV_EXAMPLE (or minimal template) + generated secrets"
    log "[DRY-RUN]   POSTGRES_PASSWORD, JWT_SIGNING_KEY, AUDIT_SIGNING_KEY, OAOS_ENCRYPTION_KEY would be generated"
    if [[ -n "$DOMAIN" ]]; then log "[DRY-RUN]   DOMAIN=$DOMAIN EMAIL=$EMAIL would be written to .env"; fi
  else
    if [[ -f "$ENV_EXAMPLE" ]]; then
      cp "$ENV_EXAMPLE" "$ENV_FILE"
    else
      cat > "$ENV_FILE" <<'ENVEOF'
TENANT_ID=default
DATABASE_URL=postgresql+asyncpg://open_agent_os:secret@localhost:5432/open_agent_os
REDIS_URL=redis://localhost:6379/0
JWT_SIGNING_KEY=change-me
AUDIT_SIGNING_KEY=change-me
VAULT_ENCRYPTION_KEY=change-me
HERMES_BASE_URL=http://localhost:8001
ENVEOF
    fi
    # Replace placeholder secrets if still default
    PG_PASS="$(gen_secret)"
    JWT_KEY="$(gen_hex)"
    AUDIT_KEY="$(gen_hex)"
    ENC_KEY="$(gen_secret)"
    # Only replace if file contains change-me markers
    if grep -q "change-me" "$ENV_FILE"; then
      # Use python for safe replacement to avoid sed delimiter issues
      python3 <<PYEOF
import pathlib, secrets, base64, re
p = pathlib.Path("$ENV_FILE")
t = p.read_text()
replacements = {
  "POSTGRES_PASSWORD": "$PG_PASS",
  "JWT_SIGNING_KEY": "$JWT_KEY",
  "AUDIT_SIGNING_KEY": "$AUDIT_KEY",
  "VAULT_ENCRYPTION_KEY": "$ENC_KEY",
  "OAOS_ENCRYPTION_KEY": "$ENC_KEY",
}
# Replace change-me values for known keys
for k, v in replacements.items():
    # match KEY=change-me...  -> KEY=v
    t = re.sub(rf"^{k}=.*change-me.*$", f"{k}={v}", t, flags=re.MULTILINE)
# Also handle bare change-me-32... patterns for JWT/AUDIT
if "JWT_SIGNING_KEY=change-me" in t or "JWT_SIGNING_KEY" not in t:
    pass
# Ensure required keys exist
for k, v in replacements.items():
    if f"{k}=" not in t:
        t += f"\n{k}={v}\n"
p.write_text(t)
print("secrets injected")
PYEOF
      # Also ensure POSTGRES_PASSWORD has safe value; inject if missing
      if ! grep -q "^POSTGRES_PASSWORD=" "$ENV_FILE"; then
        echo "POSTGRES_PASSWORD=${PG_PASS}" >> "$ENV_FILE"
      fi
      if ! grep -q "^OAOS_ENCRYPTION_KEY=" "$ENV_FILE" && grep -q "^VAULT_ENCRYPTION_KEY=" "$ENV_FILE"; then
        echo "OAOS_ENCRYPTION_KEY=${ENC_KEY}" >> "$ENV_FILE"
      fi
      # shellcheck disable=SC2016
      if ! grep -q "^OAOS_SIGNING_KEY=" "$ENV_FILE"; then
        echo "OAOS_SIGNING_KEY=${AUDIT_KEY}" >> "$ENV_FILE"
      fi
    fi
    if [[ -n "$DOMAIN" ]]; then
      # Upsert DOMAIN / TLS settings
      if grep -q "^DOMAIN=" "$ENV_FILE"; then
        sed -i "s/^DOMAIN=.*/DOMAIN=${DOMAIN}/" "$ENV_FILE"
      else
        echo "DOMAIN=${DOMAIN}" >> "$ENV_FILE"
      fi
      if [[ -n "$EMAIL" ]]; then
        if grep -q "^ACME_EMAIL=" "$ENV_FILE"; then
          sed -i "s/^ACME_EMAIL=.*/ACME_EMAIL=${EMAIL}/" "$ENV_FILE"
        else
          echo "ACME_EMAIL=${EMAIL}" >> "$ENV_FILE"
        fi
        if grep -q "^EMAIL=" "$ENV_FILE"; then
          sed -i "s/^EMAIL=.*/EMAIL=${EMAIL}/" "$ENV_FILE"
        else
          echo "EMAIL=${EMAIL}" >> "$ENV_FILE"
        fi
      fi
    fi
    chmod 600 "$ENV_FILE"
    info "  Generated $ENV_FILE (0600)"
  fi
fi

# ── 5. docker compose up ─────────────────────────────────────────
info "Step 5/8: docker compose up (deploy/docker-compose.prod.yml)"
COMPOSE_FILE="${REPO_ROOT}/deploy/docker-compose.prod.yml"
if [[ ! -f "$COMPOSE_FILE" ]]; then
  COMPOSE_FILE="${REPO_ROOT}/deploy/docker-compose.dev.yml"
fi
if [[ $DRY_RUN -eq 1 ]]; then
  log "[DRY-RUN] Would run: $COMPOSE -f $COMPOSE_FILE up -d --build"
  log "[DRY-RUN] Would wait for postgres/redis health"
else
  if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "[ERROR] No compose file found (checked prod + dev)" >&2; exit 1
  fi
  # shellcheck disable=SC2086
  $COMPOSE -f "$COMPOSE_FILE" up -d --build
  info "  compose up done"
fi

# ── 6. alembic migrate ───────────────────────────────────────────
info "Step 6/8: alembic upgrade head"
if [[ $DRY_RUN -eq 1 ]]; then
  log "[DRY-RUN] Would run: alembic upgrade head (or docker exec oaos-control-plane alembic upgrade head)"
else
  if [[ -f "${REPO_ROOT}/alembic.ini" ]]; then
    # Prefer running inside container if available, else local
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "oaos-control-plane"; then
      docker exec oaos-control-plane alembic upgrade head 2>&1 || {
        warn "  docker exec alembic failed — trying local alembic"
        (cd "$REPO_ROOT" && alembic upgrade head) || warn "  alembic upgrade failed (may need DB ready)"
      }
    else
      (cd "$REPO_ROOT" && alembic upgrade head) || warn "  alembic upgrade failed (DB may not be ready yet)"
    fi
    info "  alembic done (or attempted)"
  else
    warn "  alembic.ini not found — skipping migration"
  fi
fi

# ── 7. Health check ──────────────────────────────────────────────
info "Step 7/8: Health check (control-plane 8000, execution-gateway 8001, security 8002)"
HEALTH_URLS=(
  "http://127.0.0.1:8000/health"
  "http://127.0.0.1:8001/health"
  "http://127.0.0.1:8002/health"
)
if [[ $DRY_RUN -eq 1 ]]; then
  for u in "${HEALTH_URLS[@]}"; do
    log "[DRY-RUN] Would GET $u (expect 200)"
  done
  if [[ -x "${SCRIPT_DIR}/health-check.sh" ]]; then
    log "[DRY-RUN] Would run: ${SCRIPT_DIR}/health-check.sh"
  fi
else
  # Give services a moment to start
  sleep 3
  FAIL=0
  for u in "${HEALTH_URLS[@]}"; do
    if curl -sf --max-time 5 "$u" >/dev/null 2>&1; then
      info "  [OK] $u"
    elif docker exec oaos-control-plane python -c "import urllib.request; urllib.request.urlopen('$u', timeout=3)" 2>/dev/null; then
      info "  [OK] $u (via container)"
    else
      warn "  [MISS] $u — service may still be starting (check: $COMPOSE -f $COMPOSE_FILE ps; docker logs oaos-...)"
      FAIL=1
    fi
  done
  if [[ -x "${SCRIPT_DIR}/health-check.sh" ]]; then
    bash "${SCRIPT_DIR}/health-check.sh" || warn "health-check.sh reported issues"
  fi
  if [[ $FAIL -eq 1 ]]; then
    warn "Some health checks missed — services may still be starting; re-run: ./deploy/scripts/health-check.sh"
  fi
fi

# ── 8. Print URLs ────────────────────────────────────────────────
info "Step 8/8: Done — URLs"
if [[ -n "$DOMAIN" ]]; then
  echo ""
  echo "  Open Agent OS (Managed) — https://${DOMAIN}"
  echo "    Health:           https://${DOMAIN}/health"
  echo "    Control Plane:    https://${DOMAIN}/v1/sessions"
  echo "    Execution GW:     https://${DOMAIN}/v1/execution"
  echo "    Security API:     https://${DOMAIN}/v1/policy  /v1/audit  /v1/delegation"
  if [[ -n "$EMAIL" ]]; then
    echo "    TLS:              Let's Encrypt via ${EMAIL} (ensure DNS A -> this VPS and port 80/443 open)"
  else
    echo "    TLS:              DOMAIN set but EMAIL empty — configure certs manually or re-run with --email"
  fi
else
  echo ""
  echo "  Open Agent OS (Managed) — local"
  echo "    Control Plane:    http://127.0.0.1:8000  (or http://<VPS_IP>:8000 if ports exposed)"
  echo "    Execution GW:     http://127.0.0.1:8001"
  echo "    Security API:     http://127.0.0.1:8002"
  echo "    Health:           http://127.0.0.1:8000/health"
  echo "  Tip: re-run with --domain <FQDN> --email <addr> to enable TLS/ingress"
fi
echo ""
echo "  Compose:  $COMPOSE -f $COMPOSE_FILE ps"
echo "  Logs:     $COMPOSE -f $COMPOSE_FILE logs -f"
echo "  Health:   ./deploy/scripts/health-check.sh"
echo "  Upgrade:  ./deploy/scripts/upgrade.sh"
echo "  Backup:   ./deploy/scripts/backup.sh"
echo "  Uninstall: sudo ./deploy/scripts/uninstall.sh"
if [[ $DRY_RUN -eq 1 ]]; then echo ""; echo "[DRY-RUN] No changes were made."; fi
