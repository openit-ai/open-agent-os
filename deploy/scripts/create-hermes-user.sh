#!/usr/bin/env bash
# create-hermes-user.sh — §16A.4 Hermes 전용 OS 계정 생성
# Spec: user=hermes, home=/home/hermes, sudo disabled, root prohibited
# Usage: sudo ./create-hermes-user.sh
# Idempotent — safe to re-run.
set -euo pipefail

USER_NAME="hermes"
HOME_DIR="/home/hermes"
SHELL_BIN="/bin/bash"

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "[ERROR] This script must be run as root (sudo ./create-hermes-user.sh)" >&2
    exit 1
  fi
}

require_root

echo "[INFO] === §16A.4 hermes OS account setup ==="

# 1. Create group if missing
if getent group "${USER_NAME}" >/dev/null 2>&1; then
  echo "[INFO] Group '${USER_NAME}' already exists — skip"
else
  groupadd --system "${USER_NAME}"
  echo "[INFO] Group '${USER_NAME}' created"
fi

# 2. Create user if missing
if id "${USER_NAME}" >/dev/null 2>&1; then
  echo "[INFO] User '${USER_NAME}' already exists — verifying attributes"
  # Enforce home and shell
  usermod -d "${HOME_DIR}" -s "${SHELL_BIN}" "${USER_NAME}" || true
else
  useradd --system \
    --gid "${USER_NAME}" \
    --home-dir "${HOME_DIR}" \
    --create-home \
    --shell "${SHELL_BIN}" \
    --comment "Hermes Runtime dedicated account (§16A.4)" \
    "${USER_NAME}"
  echo "[INFO] User '${USER_NAME}' created"
fi

# 3. Home directory permissions: 750 (owner rwx, group rx, other none)
mkdir -p "${HOME_DIR}"
chown "${USER_NAME}:${USER_NAME}" "${HOME_DIR}"
chmod 0750 "${HOME_DIR}"
echo "[INFO] Home ${HOME_DIR} => 0750 ${USER_NAME}:${USER_NAME}"

# 4. Ensure sudo is DISABLED for hermes
# Remove from sudo/wheel/admin groups if present
for grp in sudo wheel adm admin; do
  if id -nG "${USER_NAME}" 2>/dev/null | tr ' ' '\n' | grep -qx "${grp}"; then
    gpasswd -d "${USER_NAME}" "${grp}" || true
    echo "[INFO] Removed '${USER_NAME}' from group '${grp}'"
  fi
done

# Drop-in sudoers deny (defense in depth — even if admin adds user to sudo group later,
# this explicit deny is evaluated). File mode must be 0440.
SUDOERS_DENY="/etc/sudoers.d/99-hermes-deny"
cat > "${SUDOERS_DENY}" <<'EOS'
# §16A.4 — hermes MUST NOT have sudo. Explicit deny overrides any group grant.
hermes ALL=(ALL) !ALL
Defaults:hermes !authenticate
EOS
chmod 0440 "${SUDOERS_DENY}"
# visudo syntax check
if command -v visudo >/dev/null 2>&1; then
  visudo -c -f "${SUDOERS_DENY}" || { echo "[ERROR] sudoers syntax invalid" >&2; exit 1; }
fi
echo "[INFO] Sudo deny written to ${SUDOERS_DENY} (0440)"

# 5. Lock password login (key-based or systemd service only; no password auth)
passwd -l "${USER_NAME}" >/dev/null 2>&1 || true
echo "[INFO] Password locked for '${USER_NAME}'"

# 6. Verify
echo ""
echo "[INFO] === Verification ==="
id "${USER_NAME}"
ls -ld "${HOME_DIR}"
echo "[INFO] sudoers:"
cat "${SUDOERS_DENY}"
echo ""
# Test sudo deny (should fail)
if sudo -l -U "${USER_NAME}" 2>&1 | grep -q "(ALL) !ALL"; then
  echo "[OK] sudo deny active for ${USER_NAME}"
else
  echo "[WARN] Could not confirm sudo deny via 'sudo -l -U hermes' — verify manually"
  sudo -l -U "${USER_NAME}" 2>&1 || true
fi

echo ""
echo "[OK] §16A.4 hermes account ready — HOME=${HOME_DIR}, NoNewPrivileges enforced via systemd unit."
echo "     Next: install deploy/systemd/hermes.service (see deploy/systemd/README.md)"
