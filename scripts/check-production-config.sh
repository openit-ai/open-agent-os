#!/usr/bin/env bash
# scripts/check-production-config.sh — friendly production preflight for systemd
# Verifies required env vars for OAOS systemd units without leaking secrets.
# - Checks file existence & permissions (0600 recommended)
# - Validates required keys, placeholder rejection, length & OAOS_ENV=production fail-closed
# - Never prints secret values (only masked length / presence)
# Exit codes: 0 = OK, 1 = missing/weak config, 2 = file not found / unreadable
#
# Usage:
#   bash scripts/check-production-config.sh                          # auto-discovers env file
#   bash scripts/check-production-config.sh --env-file /etc/oaos/oaos.env
#   bash scripts/check-production-config.sh --env-file config/oaos.env --strict
#   OAOS_ENV=production bash scripts/check-production-config.sh --env-file .env
set -euo pipefail

# --- resolve repo root -------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -d "${SCRIPT_DIR}/../config" ]]; then
  REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
elif [[ -d "${SCRIPT_DIR}/config" ]]; then
  REPO_ROOT="${SCRIPT_DIR}"
else
  REPO_ROOT="$(pwd)"
fi

ENV_FILE=""
STRICT=0
VERBOSE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --strict) STRICT=1; shift ;;
    --verbose|-v) VERBOSE=1; shift ;;
    --help|-h)
      cat <<'HELP'
Usage: check-production-config.sh [options]
  --env-file PATH   Explicit env file (default: auto-discover)
  --strict          Fail on warnings (e.g., world-readable file)
  --verbose         Show OK lines for every key
  --help            This help

Auto-discovery order:
  1) --env-file argument
  2) $OAOS_ENV_FILE
  3) /etc/oaos/oaos.env
  4) $REPO_ROOT/config/oaos.env
  5) $REPO_ROOT/control-plane/.env
  6) $REPO_ROOT/.env

Checks (fail-closed, no secret output):
  - File exists & readable; warns if >0600
  - Required: DATABASE_URL, JWT_SIGNING_KEY, AUDIT_SIGNING_KEY,
              ADMIN_JWT_SECRET, VAULT_ENCRYPTION_KEY|OAOS_ENCRYPTION_KEY,
              OAOS_ENV=production
  - Rejects placeholders: CHANGE_ME*, empty, "secret", "dev-*", length <32 for signing keys
  - Reports variable name + file location, never the value
HELP
      exit 0 ;;
    *) echo "[WARN] Unknown arg: $1" >&2; shift ;;
  esac
done

# --- auto-discover env file --------------------------------------------
if [[ -z "${ENV_FILE}" ]]; then
  if [[ -n "${OAOS_ENV_FILE:-}" && -f "${OAOS_ENV_FILE}" ]]; then
    ENV_FILE="${OAOS_ENV_FILE}"
  elif [[ -f "/etc/oaos/oaos.env" ]]; then
    ENV_FILE="/etc/oaos/oaos.env"
  elif [[ -f "${REPO_ROOT}/config/oaos.env" ]]; then
    ENV_FILE="${REPO_ROOT}/config/oaos.env"
  elif [[ -f "${REPO_ROOT}/control-plane/.env" ]]; then
    ENV_FILE="${REPO_ROOT}/control-plane/.env"
  elif [[ -f "${REPO_ROOT}/.env" ]]; then
    ENV_FILE="${REPO_ROOT}/.env"
  else
    ENV_FILE="${REPO_ROOT}/config/oaos.env.example"
  fi
fi

# --- helpers -----------------------------------------------------------
RED="$(printf '\033[31m' 2>/dev/null || true)"
GREEN="$(printf '\033[32m' 2>/dev/null || true)"
YELLOW="$(printf '\033[33m' 2>/dev/null || true)"
DIM="$(printf '\033[2m' 2>/dev/null || true)"
RESET="$(printf '\033[0m' 2>/dev/null || true)"

pass=0; fail=0; warn=0

ok()   { pass=$((pass+1)); if [[ $VERBOSE -eq 1 ]]; then echo "${GREEN}[OK]${RESET} $*"; fi; }
info() { echo "${DIM}[INFO]${RESET} $*"; }
warn_msg() { warn=$((warn+1)); echo "${YELLOW}[WARN]${RESET} $*"; }
err()  { fail=$((fail+1)); echo "${RED}[ERROR]${RESET} $*"; }

# mask: show only length, never value
masked_len() {
  local v="$1"
  if [[ -z "$v" ]]; then echo "empty"; else echo "len=${#v}"; fi
}

# parse .env without sourcing secrets into shell (avoid leaking via `set -x`)
# supports: KEY=val, KEY="val", KEY='val', inline # comment, export KEY=val
parse_env_file() {
  local file="$1"
  # Use python to parse strictly and avoid shell expansion
  python3 - "$file" <<'PY'
import sys, re, pathlib
path = sys.argv[1]
env = {}
try:
    text = pathlib.Path(path).read_text(encoding="utf-8", errors="ignore")
except Exception as e:
    print(f"PARSE_ERROR:{e}", file=sys.stderr)
    sys.exit(2)
for lineno, raw in enumerate(text.splitlines(), 1):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    # strip inline comment not inside quotes
    # simple: split on # only if not inside quotes (approx)
    # We do stateful scan
    in_s = in_d = False
    cut = None
    for i, ch in enumerate(raw):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d:
            cut = i
            break
    if cut is not None:
        raw = raw[:cut]
    raw = raw.strip()
    if raw.startswith("export "):
        raw = raw[len("export "):].strip()
    if "=" not in raw:
        continue
    k, v = raw.split("=", 1)
    k = k.strip()
    v = v.strip()
    # unquote
    if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
        v = v[1:-1]
    env[k] = v
for k, v in env.items():
    # Use NUL separator to preserve special chars; encode as repr via python then decode in bash
    # Instead, emit KEY=VALUE with tab separator and escape newlines
    # For bash consumption we use python to validate directly, but also emit for shell if needed
    pass
# Write a simple key=value file for bash to source safely via python-generated eval
import os, json
# Emit JSON to stdout for bash to consume via python caller
print(json.dumps(env))
PY
}

# --- file existence & permissions --------------------------------------
if [[ ! -f "${ENV_FILE}" ]]; then
  err "Env file not found: ${ENV_FILE}"
  echo "  → Create it first:"
  echo "     cp ${REPO_ROOT}/config/oaos.env.example ${REPO_ROOT}/config/oaos.env"
  echo "     # or"
  echo "     sudo mkdir -p /etc/oaos && sudo cp ${REPO_ROOT}/config/oaos.env.example /etc/oaos/oaos.env"
  echo "     sudo chmod 600 /etc/oaos/oaos.env && vi /etc/oaos/oaos.env"
  echo "  Searched: OAOS_ENV_FILE, /etc/oaos/oaos.env, config/oaos.env, control-plane/.env, .env"
  exit 2
fi

if [[ ! -r "${ENV_FILE}" ]]; then
  err "Env file not readable: ${ENV_FILE} (check permissions / owner)"
  exit 2
fi

info "Checking env file: ${ENV_FILE}"

# permission check (0600 ideal)
if command -v stat >/dev/null 2>&1; then
  perms="$(stat -c %a "${ENV_FILE}" 2>/dev/null || stat -f %A "${ENV_FILE}" 2>/dev/null || echo "unknown")"
  if [[ "${perms}" != "600" && "${perms}" != "400" && "${perms}" != "unknown" ]]; then
    warn_msg "File permissions are ${perms} (expected 0600). Fix: chmod 600 ${ENV_FILE}"
    if [[ $STRICT -eq 1 ]]; then
      err "Strict mode: world-readable env file rejected"
    fi
  else
    ok "File permissions ${perms} (secure)"
  fi
fi

# Parse env file via python (no secret exposure)
ENV_JSON="$(parse_env_file "${ENV_FILE}" 2>&1)" || {
  err "Failed to parse env file: ${ENV_FILE}"
  echo "  Details: ${ENV_JSON}"
  exit 2
}

# Use python for all validation so bash never holds secret values in variables that could leak via `set -x`
python3 - "$ENV_FILE" "$ENV_JSON" "$STRICT" "$VERBOSE" <<'PY'
import sys, json, re, os

env_file = sys.argv[1]
env_json = sys.argv[2]
strict = sys.argv[3] == "1"
verbose = sys.argv[4] == "1"

try:
    env = json.loads(env_json)
except Exception as e:
    print(f"[ERROR] JSON parse failed: {e}", file=sys.stderr)
    sys.exit(2)

RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; DIM = "\033[2m"; RESET = "\033[0m"

def ok(msg):
    if verbose:
        print(f"{GREEN}[OK]{RESET} {msg}")
def warn(msg):
    print(f"{YELLOW}[WARN]{RESET} {msg}")
def err(msg):
    print(f"{RED}[ERROR]{RESET} {msg}")

failures = 0
warnings = 0

def masked_len(v):
    return f"len={len(v)}" if v else "empty"

def is_placeholder(v: str) -> bool:
    if v is None:
        return True
    s = v.strip()
    if not s:
        return True
    low = s.lower()
    placeholders = ["change_me", "changeme", "replace_me", "todo", "example", "your_", "secret"]
    # Check prefix or contains
    if any(p in low for p in ["change_me", "changeme"]):
        return True
    # Literal weak dev defaults
    if s in ("secret", "password", "changeme", "dev-only-change-me", "dev-signing-key-please-change"):
        return True
    if low.startswith("dev-") and len(s) < 32:
        return True
    return False

def check_required(key, min_len=0, alternatives=None):
    global failures, warnings
    val = env.get(key)
    # alternatives: e.g., VAULT_ENCRYPTION_KEY can be satisfied by OAOS_ENCRYPTION_KEY
    if alternatives:
        if val and not is_placeholder(val) and (min_len == 0 or len(val.strip()) >= min_len):
            ok(f"{key} present ({masked_len(val.strip())}) — {env_file}")
            return True
        for alt in alternatives:
            aval = env.get(alt)
            if aval and not is_placeholder(aval) and (min_len == 0 or len(aval.strip()) >= min_len):
                ok(f"{key} satisfied via {alt} ({masked_len(aval.strip())}) — {env_file}")
                return True
        # none satisfied
        print(f"{RED}[ERROR]{RESET} Missing or weak: {key} (also checked {', '.join(alternatives)}) — file: {env_file}")
        print(f"  → Fix: set {key} (or {alternatives[0]}) in {env_file} to a strong value (≥{min_len} chars, not CHANGE_ME)")
        print(f"     Generate: openssl rand -base64 32")
        failures += 1
        return False
    else:
        if val is None or is_placeholder(val):
            print(f"{RED}[ERROR]{RESET} Missing or placeholder: {key} — file: {env_file} ({masked_len(val or '')})")
            print(f"  → Fix: set {key} in {env_file} (≥{min_len} chars, not CHANGE_ME)")
            if min_len >= 32:
                print(f"     Generate: openssl rand -base64 32")
            failures += 1
            return False
        if min_len and len(val.strip()) < min_len:
            print(f"{RED}[ERROR]{RESET} Too short: {key} — file: {env_file} ({masked_len(val.strip())}, need ≥{min_len})")
            print(f"  → Fix: replace {key} in {env_file} with a longer value (≥{min_len} chars)")
            failures += 1
            return False
        ok(f"{key} present ({masked_len(val.strip())}) — {env_file}")
        return True

# --- Required checks ---------------------------------------------------
# DATABASE_URL: accept global/admin/control-plane-prefixed forms used by systemd.
# The selected URL must not be a placeholder and must contain postgresql/postgres.
_db_key = ""
for _candidate in ("DATABASE_URL", "OAOS_DATABASE_URL", "OAOS_CP_DATABASE_URL"):
    if env.get(_candidate):
        _db_key = _candidate
        break
_db = env.get(_db_key) or ""
db = _db
if not _db or is_placeholder(_db):
    print(f"{RED}[ERROR]{RESET} Missing or placeholder: DATABASE_URL — file: {env_file}")
    print(f"  → Fix: set DATABASE_URL (or OAOS_DATABASE_URL / OAOS_CP_DATABASE_URL) in {env_file}")
    print(f"     Example: DATABASE_URL=postgresql+asyncpg://oaos:STRONG_PASSWORD@localhost:5432/oaos")
    failures += 1
else:
    if "CHANGE_ME" in db or "secret" in db.lower() and "CHANGE_ME" in db:
        print(f"{RED}[ERROR]{RESET} DATABASE_URL contains placeholder (CHANGE_ME) — file: {env_file}")
        failures += 1
    elif "postgresql" not in db and "postgres" not in db:
        print(f"{YELLOW}[WARN]{RESET} DATABASE_URL does not look like postgresql URL — file: {env_file}")
        warnings += 1
    else:
        ok(f"DATABASE_URL present ({masked_len(db)}) — {env_file}")

# Signing keys
check_required("JWT_SIGNING_KEY", min_len=32)
check_required("AUDIT_SIGNING_KEY", min_len=32)
check_required("ADMIN_JWT_SECRET", min_len=32)
# Vault key: either VAULT_ENCRYPTION_KEY or OAOS_ENCRYPTION_KEY
check_required("VAULT_ENCRYPTION_KEY", min_len=32, alternatives=["OAOS_ENCRYPTION_KEY"])

# OAOS_ENV must be production
oaos_env = (env.get("OAOS_ENV") or env.get("ENV") or env.get("OAOS_ENVIRONMENT") or "").strip().lower()
if oaos_env in ("production", "prod"):
    ok(f"OAOS_ENV={oaos_env} (production) — {env_file}")
else:
    print(f"{RED}[ERROR]{RESET} OAOS_ENV must be 'production' for systemd production — file: {env_file} (found: '{oaos_env or 'empty'}')")
    print(f"  → Fix: set OAOS_ENV=production in {env_file}")
    failures += 1

# Optional warnings
if not env.get("REDIS_URL"):
    print(f"{YELLOW}[WARN]{RESET} REDIS_URL not set — file: {env_file} (will default to redis://localhost:6379/0)")
    warnings += 1
else:
    if "CHANGE_ME" in env.get("REDIS_URL",""):
        print(f"{RED}[ERROR]{RESET} REDIS_URL contains placeholder — file: {env_file}")
        failures += 1
    else:
        ok(f"REDIS_URL present — {env_file}")

cors = env.get("OAOS_CORS_ORIGINS","")
if cors and "localhost" in cors and oaos_env in ("production","prod"):
    print(f"{YELLOW}[WARN]{RESET} OAOS_CORS_ORIGINS still contains localhost in production — file: {env_file}")
    print(f"  → Fix: replace with real origins (e.g., https://oaos.example.com)")
    warnings += 1

# Check for dev defaults that must not appear in production
dev_markers = ["dev-only-change-me", "dev-signing-key-please-change", "secret"]
for k, v in env.items():
    if v and any(m in v for m in dev_markers) and k in ("JWT_SIGNING_KEY","AUDIT_SIGNING_KEY","ADMIN_JWT_SECRET","VAULT_ENCRYPTION_KEY","OAOS_ENCRYPTION_KEY"):
        # already caught by is_placeholder but add explicit hint
        pass

# Summary
print("")
if failures == 0 and warnings == 0:
    print(f"{GREEN}[OK]{RESET} All required production config present — {env_file} (no secrets printed)")
    sys.exit(0)
elif failures == 0:
    print(f"{YELLOW}[WARN]{RESET} Preflight passed with {warnings} warning(s) — {env_file} (no secrets printed)")
    print(f"  Review warnings above. Use --strict to fail on warnings.")
    sys.exit(0 if not strict else 1)
else:
    print(f"{RED}[FAIL]{RESET} Preflight failed: {failures} error(s), {warnings} warning(s) — {env_file} (no secrets printed)")
    print(f"  Fix the [ERROR] lines above, then re-run: bash scripts/check-production-config.sh --env-file {env_file}")
    sys.exit(1)
PY
RC=$?
if [[ $RC -ne 0 ]]; then
  exit $RC
fi
# Also check that referenced Python interpreter exists (non-blocking warning)
if ! command -v python3 >/dev/null 2>&1; then
  warn_msg "python3 not found in PATH (required for services)"
fi
# Final summary line for automation (machine-friendly)
if [[ $fail -eq 0 ]]; then
  # python already printed summary; add concise line
  true
fi
