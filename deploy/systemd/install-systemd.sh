#!/usr/bin/env bash
# deploy/systemd/install-systemd.sh — systemd production installer (friendly, no credential invention)
# Installs OAOS Control Plane (8100) as systemd unit; optionally Execution/Security if verified.
# - NEVER invents credentials: if env file has placeholders/missing secrets, abort with clear guidance
# - Runs check-production-config.sh preflight before touching systemd
# - Supports system units (/etc/systemd/system) and user units (~/.config/systemd/user)
# - Idempotent, dry-run friendly
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CHECK_SCRIPT="${REPO_ROOT}/scripts/check-production-config.sh"
if [[ ! -f "${CHECK_SCRIPT}" ]]; then
  CHECK_SCRIPT="${SCRIPT_DIR}/check-production-config.sh"
fi

ENV_FILE=""
MODE="system"
DRY_RUN=0
ENABLE_NOW=1
ONLY_CONTROL_PLANE=0
WITH_OPTIONAL=0

usage() {
  cat <<'HELP'
Usage: install-systemd.sh [options]
  --env-file PATH      Env file (default: auto-discover via check script:
                       /etc/oaos/oaos.env, config/oaos.env, control-plane/.env, .env)
  --system             Install to /etc/systemd/system (default, requires sudo)
  --user               Install to ~/.config/systemd/user (no sudo, port 8100 user unit)
  --with-optional      Also install execution-gateway (8001) & security (8002) units
  --only-control-plane Only install control-plane unit (default without --with-optional)
  --dry-run            Print actions without executing
  --no-enable          Copy units but do not daemon-reload / enable / start
  -h, --help           This help

Examples:
  # System (production, as on 192.168.6.61):
  sudo mkdir -p /etc/oaos && sudo cp config/oaos.env.example /etc/oaos/oaos.env
  sudo chmod 600 /etc/oaos/oaos.env && sudo vi /etc/oaos/oaos.env  # replace CHANGE_ME_*
  sudo bash deploy/systemd/install-systemd.sh --env-file /etc/oaos/oaos.env
  sudo bash deploy/systemd/install-systemd.sh --env-file /etc/oaos/oaos.env --with-optional

  # User (developer, no sudo, mirrors current 192.168.6.61 user unit :8100):
  cp config/oaos.env.example config/oaos.env && vi config/oaos.env
  bash deploy/systemd/install-systemd.sh --user --env-file config/oaos.env
  # Optional: with execution/security (verified entrypoints only)
  bash deploy/systemd/install-systemd.sh --user --with-optional --dry-run

Security:
  - Never invents credentials. If check-production-config.sh reports missing/placeholder
    secrets, install aborts and prints remediation (never prints secret values).
  - Env file must be 0600. Units reference EnvironmentFile without embedding secrets.
HELP
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --system) MODE="system"; shift ;;
    --user) MODE="user"; shift ;;
    --with-optional) WITH_OPTIONAL=1; shift ;;
    --only-control-plane) ONLY_CONTROL_PLANE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-enable) ENABLE_NOW=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "[WARN] Unknown arg: $1" >&2; shift ;;
  esac
done

log()  { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2; }
info() { log "[INFO] $*"; }
warn() { log "[WARN] $*"; }
die()  { log "[ERROR] $*"; exit 1; }

run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would run: $*"
  else
    "$@"
  fi
}

# --- 0. Discover env file for preflight ---------------------------------
ENV_ARG=()
if [[ -n "${ENV_FILE}" ]]; then
  ENV_ARG=(--env-file "${ENV_FILE}")
  # also export for check script auto-discovery
  export OAOS_ENV_FILE="${ENV_FILE}"
else
  # Let check script auto-discover; capture its choice for later
  if [[ -f "/etc/oaos/oaos.env" ]]; then ENV_FILE="/etc/oaos/oaos.env"
  elif [[ -f "${REPO_ROOT}/config/oaos.env" ]]; then ENV_FILE="${REPO_ROOT}/config/oaos.env"
  elif [[ -f "${REPO_ROOT}/control-plane/.env" ]]; then ENV_FILE="${REPO_ROOT}/control-plane/.env"
  elif [[ -f "${REPO_ROOT}/.env" ]]; then ENV_FILE="${REPO_ROOT}/.env"
  else ENV_FILE="${REPO_ROOT}/config/oaos.env.example"
  fi
  ENV_ARG=(--env-file "${ENV_FILE}")
fi

info "Repo root: ${REPO_ROOT}"
info "Env file: ${ENV_FILE}"
info "Mode: ${MODE} (dry-run=${DRY_RUN}, with-optional=${WITH_OPTIONAL})"

# --- 1. Preflight: friendly config check (no secret output, no invention) -
info "Step 1/5: Preflight — check-production-config.sh (no secret output)"
if [[ ! -f "${CHECK_SCRIPT}" ]]; then
  die "Check script not found: ${CHECK_SCRIPT}"
fi
if ! bash "${CHECK_SCRIPT}" "${ENV_ARG[@]}"; then
  echo "" >&2
  die "Preflight failed — fix the [ERROR] lines above before installing.

  Quick fix:
    cp ${REPO_ROOT}/config/oaos.env.example ${REPO_ROOT}/config/oaos.env
    chmod 600 ${REPO_ROOT}/config/oaos.env
    vi ${REPO_ROOT}/config/oaos.env  # replace every CHANGE_ME_* (see header comments)
    # generate strong secrets:
    #   openssl rand -base64 32
  Then re-run:
    bash ${CHECK_SCRIPT} --env-file <your-env-file>
  And re-run this installer.

  Never commit the real env file. The installer NEVER invents credentials."
fi
info "Preflight OK — no secrets printed, all required keys present and strong."

# --- 2. Detect python interpreter ----------------------------------------
detect_python() {
  # Priority: OAOS_PYTHON env > hermes venv > python3.12 > python3
  if [[ -n "${OAOS_PYTHON:-}" && -x "${OAOS_PYTHON}" ]]; then
    echo "${OAOS_PYTHON}"; return 0
  fi
  local candidates=(
    "${HOME}/.hermes/hermes-agent/venv/bin/python"
    "/home/openitsvc/.hermes/hermes-agent/venv/bin/python"
    "/usr/bin/python3.12"
    "/usr/bin/python3"
  )
  for p in "${candidates[@]}"; do
    if [[ -x "$p" ]]; then
      # verify it can import fastapi
      if "$p" -c "import fastapi" 2>/dev/null; then
        echo "$p"; return 0
      fi
    fi
  done
  # fallback: first existing
  for p in "${candidates[@]}"; do
    if [[ -x "$p" ]]; then echo "$p"; return 0; fi
  done
  echo "/usr/bin/python3"
}
PYTHON_BIN="$(detect_python)"
info "Python interpreter: ${PYTHON_BIN}"

# --- 3. Resolve install destinations -------------------------------------
if [[ "${MODE}" == "user" ]]; then
  DEST_DIR="${HOME}/.config/systemd/user"
  ENV_FILE_SYSTEM=""  # user units use relative %h paths; no /etc/oaos copy
else
  DEST_DIR="/etc/systemd/system"
  if [[ $DRY_RUN -eq 0 && $EUID -ne 0 ]]; then
    die "System install requires root: sudo bash deploy/systemd/install-systemd.sh"
  fi
  if [[ $DRY_RUN -eq 1 && $EUID -ne 0 ]]; then
    warn "DRY-RUN: system install without root — would require sudo in real run"
  fi
fi

info "Destination: ${DEST_DIR}"

# --- 3b. Ensure the dedicated system service identity exists -------------
if [[ "${MODE}" == "system" && $DRY_RUN -eq 0 ]]; then
  if [[ $EUID -ne 0 ]]; then
    die "System install requires root to verify/create the oaos service account: sudo bash deploy/systemd/install-systemd.sh"
  fi
  if ! getent group oaos >/dev/null 2>&1; then
    info "Creating dedicated group: oaos"
    groupadd --system oaos
  fi
  if ! id oaos >/dev/null 2>&1; then
    info "Creating dedicated service user: oaos (no login, no shell access)"
    useradd --system --gid oaos --home-dir /var/lib/oaos --create-home --shell /usr/sbin/nologin --comment "Open Agent OS systemd service" oaos
  fi
  install -d -o oaos -g oaos -m 0750 /var/lib/oaos
elif [[ "${MODE}" == "system" ]]; then
  info "DRY-RUN: would ensure dedicated system user/group oaos exists"
fi

# System units use one canonical root-owned EnvironmentFile. Copy an explicitly
# supplied file into that location; never overwrite it implicitly when omitted.
SYSTEM_ENV_FILE=/etc/oaos/oaos.env
if [[ "${MODE}" == "system" ]]; then
  if [[ "${ENV_FILE}" != "${SYSTEM_ENV_FILE}" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      log "[DRY-RUN] Would install env file ${ENV_FILE} -> ${SYSTEM_ENV_FILE} (0600, root:root)"
    else
      install -d -m 0750 /etc/oaos
      install -m 0600 -o root -g root "${ENV_FILE}" "${SYSTEM_ENV_FILE}"
      info "Installed canonical systemd env file: ${SYSTEM_ENV_FILE}"
    fi
  fi
fi

# --- 4. Install units ----------------------------------------------------
install_unit() {
  local src="$1" dest="$2"
  local dest_path="${DEST_DIR}/$(basename "${dest}")"
  info "Installing $(basename "${src}") → ${dest_path}"
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would copy ${src} → ${dest_path} and patch WorkingDirectory/Python paths"
    return 0
  fi
  mkdir -p "${DEST_DIR}"
  # Patch WorkingDirectory and PYTHONPATH to match actual REPO_ROOT, and Python bin
  # Use a temp file for substitution
  local tmp
  tmp="$(mktemp)"
  sed -e "s|WorkingDirectory=/home/openitsvc/open-agent-os|WorkingDirectory=${REPO_ROOT}|g" \
      -e "s|Environment=PYTHONPATH=/home/openitsvc/open-agent-os|Environment=PYTHONPATH=${REPO_ROOT}|g" \
      -e "s|ReadWritePaths=/home/openitsvc/open-agent-os|ReadWritePaths=${REPO_ROOT}|g" \
      -e "s|ExecStart=/usr/bin/python3 |ExecStart=${PYTHON_BIN} |g" \
      -e "s|ExecStart=/usr/bin/python3.12 |ExecStart=${PYTHON_BIN} |g" \
      "${src}" > "${tmp}"
  # Also patch hermes venv path in user unit if repo lives elsewhere
  if [[ "${MODE}" == "user" ]]; then
    # user unit uses %h paths — no patch needed
    cp "${src}" "${tmp}.user"
    mv "${tmp}.user" "${tmp}"
    # If user's hermes venv is not at %h/.hermes/... but exists, keep as is; user can edit
  fi
  # Validate
  if command -v systemd-analyze >/dev/null 2>&1; then
    if ! systemd-analyze verify "${tmp}" 2>&1 | head -n 20; then
      warn "systemd-analyze verify reported issues (see above) — continuing"
    fi
  fi
  # Install with correct perms
  install -m 0644 "${tmp}" "${dest_path}"
  rm -f "${tmp}"
  log "[OK] Installed ${dest_path}"
}

# Always install control-plane (single source of truth — :8100)
if [[ "${MODE}" == "user" ]]; then
  install_unit "${REPO_ROOT}/deploy/systemd/user/oaos-control-plane.service" "oaos-control-plane.service"
else
  install_unit "${REPO_ROOT}/deploy/systemd/oaos-control-plane.service" "oaos-control-plane.service"
fi

# Optional: execution-gateway & security (only if entrypoints verified)
if [[ $WITH_OPTIONAL -eq 1 && $ONLY_CONTROL_PLANE -eq 0 ]]; then
  # Verify entrypoints before installing
  verify_entrypoint() {
    local mod="$1" workdir="$2"
    if PYTHONPATH="${REPO_ROOT}:${workdir}" "${PYTHON_BIN}" -c "import ${mod}" 2>/dev/null; then
      return 0
    fi
    # also try hermes venv python if different
    if [[ "${PYTHON_BIN}" != "${HOME}/.hermes/hermes-agent/venv/bin/python" && -x "${HOME}/.hermes/hermes-agent/venv/bin/python" ]]; then
      if PYTHONPATH="${REPO_ROOT}:${workdir}" "${HOME}/.hermes/hermes-agent/venv/bin/python" -c "import ${mod}" 2>/dev/null; then
        return 0
      fi
    fi
    return 1
  }
  if verify_entrypoint "execution_gateway.app" "${REPO_ROOT}/execution-gateway"; then
    if [[ "${MODE}" == "user" ]]; then
      warn "No user unit for execution-gateway (system only) — skipping in --user mode. Use --system --with-optional for all three."
    else
      install_unit "${REPO_ROOT}/deploy/systemd/oaos-execution-gateway.service" "oaos-execution-gateway.service"
    fi
  else
    warn "Execution gateway entrypoint not verified — skipping oaos-execution-gateway.service (check PYTHONPATH/fastapi)"
  fi
  if verify_entrypoint "app" "${REPO_ROOT}/security"; then
    if [[ "${MODE}" == "user" ]]; then
      warn "No user unit for security (system only) — skipping in --user mode."
    else
      install_unit "${REPO_ROOT}/deploy/systemd/oaos-security.service" "oaos-security.service"
    fi
  else
    warn "Security entrypoint not verified — skipping oaos-security.service"
  fi
fi

# Ensure /etc/oaos/oaos.env has correct perms if we are in system mode and file exists
if [[ "${MODE}" == "system" && -f "${ENV_FILE}" && "${ENV_FILE}" == "/etc/oaos/oaos.env" ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] Would ensure chmod 600 /etc/oaos/oaos.env"
  else
    chmod 600 "${ENV_FILE}" || warn "Failed to chmod 600 ${ENV_FILE}"
    info "Secured ${ENV_FILE} (0600)"
  fi
fi

# --- 5. daemon-reload + enable -------------------------------------------
if [[ $ENABLE_NOW -eq 0 ]]; then
  info "Skipping daemon-reload/enable (--no-enable)"
  info "Done. Units copied to ${DEST_DIR}. Run manually:"
  if [[ "${MODE}" == "user" ]]; then
    echo "  systemctl --user daemon-reload && systemctl --user enable --now oaos-control-plane.service"
  else
    echo "  sudo systemctl daemon-reload && sudo systemctl enable --now oaos-control-plane.service"
  fi
  exit 0
fi

if [[ "${MODE}" == "user" ]]; then
  info "Step 5/5: systemctl --user daemon-reload + enable"
  run systemctl --user daemon-reload
  run systemctl --user enable oaos-control-plane.service
  if [[ $DRY_RUN -eq 0 ]]; then
    run systemctl --user restart oaos-control-plane.service || run systemctl --user start oaos-control-plane.service
    sleep 1
    systemctl --user is-active oaos-control-plane.service || warn "oaos-control-plane.service not active — check journalctl --user -u oaos-control-plane -n 50"
    info "Verify: curl -s http://127.0.0.1:8100/healthz | jq"
    info "Logs: journalctl --user -u oaos-control-plane -f"
  fi
else
  info "Step 5/5: systemctl daemon-reload + enable"
  run systemctl daemon-reload
  run systemctl enable oaos-control-plane.service
  if [[ $WITH_OPTIONAL -eq 1 ]]; then
    for u in oaos-execution-gateway.service oaos-security.service; do
      if [[ -f "${DEST_DIR}/${u}" ]]; then
        run systemctl enable "${u}" || true
      fi
    done
  fi
  if [[ $DRY_RUN -eq 0 ]]; then
    run systemctl restart oaos-control-plane.service || run systemctl start oaos-control-plane.service
    sleep 1
    systemctl is-active oaos-control-plane.service || warn "oaos-control-plane.service not active — check journalctl -u oaos-control-plane -n 50"
    info "Verify: curl -s http://127.0.0.1:8100/healthz | jq   &&   systemctl status oaos-control-plane.service"
    info "Logs: journalctl -u oaos-control-plane -f"
  fi
fi

info "Done. Installed to ${DEST_DIR}. Env file: ${ENV_FILE} (no secrets printed)."
if [[ "${MODE}" == "system" ]]; then
  info "Production ports: control-plane 8100, execution-gateway 8001, security 8002, admin-api 8010 (if installed)."
fi
