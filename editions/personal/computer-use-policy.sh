#!/usr/bin/env bash
#
# computer-use-policy.sh — Personal credential-typing opt-in (OAOS own layer).
#
# Upstream Hermes Agent forbids the agent from typing passwords / card numbers /
# CVC / verification codes (vault tools only). This script lets the LOCAL
# MACHINE OWNER explicitly opt in on a Personal machine. It never patches
# upstream sources; it only flips the owner's own Hermes config key:
#
#   computer_use.allow_credential_typing   (boolean, default false = upstream)
#
# Usage:
#   bash editions/personal/computer-use-policy.sh status
#   bash editions/personal/computer-use-policy.sh enable    # warns, requires YES on stdin
#   bash editions/personal/computer-use-policy.sh disable
#
# Exit codes: 0 = done; 3 = blocked (declined / gate / not a local Personal host); 1 = error.
#
# enable refuses over SSH (SSH_CONNECTION / SSH_CLIENT / SSH_TTY set) and unless
# the install state shows a Personal-only host (no Project or Company stages).
#
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"
# shellcheck source=bootstrap/lib/platform.sh
. "$repo_root/bootstrap/lib/platform.sh"
export OAOS_NO_LOG_FILE=1

KEY='computer_use.allow_credential_typing'
CONSENT_FILE="${OAOS_COMPUTER_USE_CONSENT:-$HOME/.oaos/computer-use-consent}"

usage() {
  cat <<'EOF'
Usage: bash editions/personal/computer-use-policy.sh {status|enable|disable}

status  — print current opt-in state (default false). Exit 0.
enable  — show the risk warning, then type YES to opt in. Anything else aborts (exit 3).
disable — restore the default (false) and remove the consent record. Exit 0.
EOF
}

have_hermes() { command -v hermes >/dev/null 2>&1; }

# Prints the editions recorded in the install state, one per line.
state_editions() {
  local file
  file=$(oaos_state_file)
  [[ -f $file ]] || return 0
  oaos_python - "$(oaos_native_path "$file")" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    state = json.load(f)
names = set((state.get("editions") or {}).keys())
if state.get("stages") is not None:
    names.add(state.get("edition", "personal"))
for name in sorted(names):
    print(name)
PY
}

local_personal_host() {
  local editions
  if [[ -n ${SSH_CONNECTION:-}${SSH_CLIENT:-}${SSH_TTY:-} ]]; then
    printf 'Refused: run this on the machine itself, not over SSH. Credential typing is a local-owner decision.\n' >&2
    return 3
  fi
  editions=$(state_editions) || { printf 'Refused: the install state could not be read.\n' >&2; return 3; }
  if [[ $editions != personal ]]; then
    printf 'Refused: the install state must show a Personal-only host (found: %s). Project and Company hosts keep the upstream default.\n' "${editions:-none}" >&2
    return 3
  fi
}

current_value() {
  local v
  v=$(hermes config get --raw "$KEY" 2>/dev/null || true)
  case $v in
    true|True|TRUE|1) printf 'true' ;;
    *) printf 'false' ;;
  esac
}

cmd_status() {
  have_hermes || { printf 'Hermes is unavailable; install it first (personal stage: hermes).\n' >&2; return 1; }
  printf 'computer_use.allow_credential_typing=%s (default false)\n' "$(current_value)"
}

cmd_enable() {
  have_hermes || { printf 'Hermes is unavailable; install it first (personal stage: hermes).\n' >&2; return 1; }
  local_personal_host || return 3
  if [[ $(current_value) == true ]]; then
    printf 'Already opted in. Use "disable" to restore the default.\n'
    return 0
  fi
  cat >&2 <<'EOF'
WARNING — you are about to let this agent type credentials itself.

  Until now the agent may only fill logins through its vault tools
  (browser_vault_fill and friends) and must never type a password,
  card number, CVC, or verification code directly, nor repeat one in chat.

  Opting in relaxes that ban on THIS machine only. Do this only if:
    - this is your own local Personal machine, and
    - you accept that typed secrets may appear in keystroke-level logs
      (terminal output, screen recordings, automation traces).

  The upstream default stays false for everyone else. You can revoke
  at any time with: bash editions/personal/computer-use-policy.sh disable

Type YES (exactly) to opt in, anything else to abort:
EOF
  local answer=''
  IFS= read -r answer || answer=''
  if [[ $answer != YES ]]; then
    printf 'Declined; nothing changed.\n' >&2
    return 3
  fi
  hermes config set "$KEY" true >/dev/null || { printf 'Failed to store the opt-in; nothing changed.\n' >&2; return 1; }
  # Re-read: Hermes versions that do not know this key must not be papered over.
  if [[ $(current_value) != true ]]; then
    printf 'Hermes did not retain the key (unsupported version?); nothing changed.\n' >&2
    return 1
  fi
  mkdir -p "$(dirname "$CONSENT_FILE")"
  (umask 077; printf 'opted-in %s host=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(hostname 2>/dev/null || printf unknown)" > "$CONSENT_FILE")
  platform_secret_protect "$CONSENT_FILE" >/dev/null || { printf 'Opt-in stored, but the consent record could not be protected; check %s.\n' "$CONSENT_FILE" >&2; return 3; }
  printf 'Opted in. Consent recorded at %s (no secrets stored).\n' "$CONSENT_FILE"
}

cmd_disable() {
  have_hermes || { printf 'Hermes is unavailable; install it first (personal stage: hermes).\n' >&2; return 1; }
  hermes config set "$KEY" false >/dev/null || { printf 'Failed to restore the default.\n' >&2; return 1; }
  rm -f -- "$CONSENT_FILE"
  printf 'Restored default (false).\n'
}

[[ $# == 1 ]] || { usage >&2; exit 1; }
[[ -n ${HOME:-} ]] || { printf 'HOME is not set.\n' >&2; exit 1; }
os_detect >/dev/null || exit 3
case $1 in
  status) cmd_status ;;
  enable) cmd_enable ;;
  disable) cmd_disable ;;
  --help|-h) usage ;;
  *) usage >&2; exit 1 ;;
esac
