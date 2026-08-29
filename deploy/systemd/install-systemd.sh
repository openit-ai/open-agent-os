#!/usr/bin/env bash
# deploy/systemd/install-systemd.sh — systemd production installer (auto-generates secrets on new install, preserves on existing)
# Installs OAOS Control Plane (8100) as systemd unit; optionally Execution/Security if verified.
# - NEW install (canonical env missing): auto-generates 64-hex secrets for JWT_SIGNING_KEY, AUDIT_SIGNING_KEY, ADMIN_JWT_SECRET, VAULT/OAOS_ENCRYPTION_KEY (aliases, same value)
# - Existing install: preserves all existing secrets unless --rotate-secrets is given
# - Weak/missing secrets on existing install => clear error unless --rotate-secrets
# - --rotate-secrets: opt-in rotation with warning, never prints secret values
# - Runs check-production-config.sh preflight before touching systemd
# - Supports system units (/etc/systemd/system) and user units (~/.config/systemd/user)
# - Idempotent, dry-run friendly; Docker path (deploy/docker-compose.*.yml + .env) unchanged
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
ROTATE_SECRETS=0

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
  --rotate-secrets     Rotate all canonical secrets (JWT_SIGNING_KEY, AUDIT_SIGNING_KEY, ADMIN_JWT_SECRET, VAULT/OAOS_ENCRYPTION_KEY); warns that existing sessions/tokens will be invalidated
  -h, --help           This help

Examples:
  # System (production, as on 192.168.6.61):
  sudo mkdir -p /etc/oaos && sudo cp config/oaos.env.example /etc/oaos/oaos.env
  sudo chmod 600 /etc/oaos/oaos.env && sudo vi /etc/oaos/oaos.env  # replace CHANGE_ME_* (secrets auto-generated on new install)
  sudo bash deploy/systemd/install-systemd.sh --env-file /etc/oaos/oaos.env
  sudo bash deploy/systemd/install-systemd.sh --env-file /etc/oaos/oaos.env --with-optional

  # User (developer, no sudo, mirrors current 192.168.6.61 user unit :8100):
  cp config/oaos.env.example config/oaos.env && vi config/oaos.env
  bash deploy/systemd/install-systemd.sh --user --env-file config/oaos.env
  # Optional: with execution/security (verified entrypoints only)
  bash deploy/systemd/install-systemd.sh --user --with-optional --dry-run

Security:
  - New install: automatically generates 64-hex secrets for missing/placeholder keys (never prints values).
  - Existing install: preserves all secrets; weak/missing secrets abort unless --rotate-secrets is given.
  - --rotate-secrets rotates all canonical secrets with a clear warning (existing JWTs/sessions invalidated).
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
    --rotate-secrets) ROTATE_SECRETS=1; shift ;;
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

# Determine canonical env file for this mode (the only file systemd units read)
CANONICAL_ENV_FILE=""
if [[ "${MODE}" == "system" ]]; then
  CANONICAL_ENV_FILE="/etc/oaos/oaos.env"
else
  CANONICAL_ENV_FILE="${REPO_ROOT}/config/oaos.env"
fi

info "Repo root: ${REPO_ROOT}"
info "Env file: ${ENV_FILE} (canonical for ${MODE}: ${CANONICAL_ENV_FILE})"
info "Mode: ${MODE} (dry-run=${DRY_RUN}, with-optional=${WITH_OPTIONAL}, rotate-secrets=${ROTATE_SECRETS})"

# --- 0b. Secret helpers: generate 64-hex, detect weak, create/rotate/preserve --
generate_hex64() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32 2>/dev/null && return 0
  fi
  python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null && return 0
  # fallback: /dev/urandom
  od -An -tx1 -N 32 /dev/urandom 2>/dev/null | tr -d ' \n' && return 0
  # last resort (insecure): date sha
  echo -n "$(date +%s%N)$$" | sha256sum | cut -d' ' -f1
}

# Python helpers to avoid leaking secrets via bash variables
is_weak_secret_py() {
  python3 - "$1" "$2" <<'PY'
import sys
val=sys.argv[2] if len(sys.argv)>2 else ""
s=(val or "").strip()
low=s.lower()
if not s: print("weak"); sys.exit(0)
if "change_me" in low or "changeme" in low: print("weak"); sys.exit(0)
if s in ("secret","password","changeme","dev-only-change-me","dev-signing-key-please-change"): print("weak"); sys.exit(0)
if low.startswith("dev-") and len(s)<32: print("weak"); sys.exit(0)
if len(s)<32: print("weak"); sys.exit(0)
print("strong")
PY
}

ensure_canonical_secrets() {
  local canonical="$1"
  local was_new=0
  local do_generate=0
  local do_rotate=0
  local reason=""

  if [[ ! -f "${canonical}" ]]; then
    was_new=1
    do_generate=1
    reason="new install — canonical ${canonical} not found"
  else
    # canonical exists: check for weak secrets
    local weak_found=0
    # use python to check each key and report weak count
    weak_found=$(python3 - "${canonical}" <<'PY'
import pathlib, json, sys
path=sys.argv[1]
text=pathlib.Path(path).read_text(encoding="utf-8", errors="ignore")
env={}
for raw in text.splitlines():
    line=raw.strip()
    if not line or line.startswith("#"): continue
    # strip inline comment outside quotes (simple)
    in_s=in_d=False
    cut=None
    for i,ch in enumerate(raw):
        if ch=="'" and not in_d: in_s=not in_s
        elif ch=='"' and not in_s: in_d=not in_d
        elif ch=="#" and not in_s and not in_d:
            cut=i; break
    if cut is not None: raw=raw[:cut]
    raw=raw.strip()
    if raw.startswith("export "): raw=raw[len("export "):].strip()
    if "=" not in raw: continue
    k,v=raw.split("=",1)
    k=k.strip(); v=v.strip()
    if len(v)>=2 and ((v[0]=='"' and v[-1]=='"') or (v[0]=="'" and v[-1]=="'")): v=v[1:-1]
    env[k]=v

def is_weak(v):
    if v is None: return True
    s=v.strip()
    if not s: return True
    low=s.lower()
    if "change_me" in low or "changeme" in low: return True
    if s in ("secret","password","changeme","dev-only-change-me","dev-signing-key-please-change"): return True
    if low.startswith("dev-") and len(s)<32: return True
    if len(s)<32: return True
    return False

keys=["JWT_SIGNING_KEY","AUDIT_SIGNING_KEY","ADMIN_JWT_SECRET"]
weak=0
for k in keys:
    v=env.get(k)
    if is_weak(v or ""):
        weak+=1
# encryption is alias: either VAULT_ENCRYPTION_KEY or OAOS_ENCRYPTION_KEY must be strong
enc_vault=env.get("VAULT_ENCRYPTION_KEY","")
enc_oaos=env.get("OAOS_ENCRYPTION_KEY","")
if is_weak(enc_vault) and is_weak(enc_oaos):
    weak+=1
print(weak)
PY
)
    weak_found=$(echo "$weak_found" | tr -d '[:space:]')
    if [[ "$weak_found" != "0" && "$weak_found" != "" ]]; then
      if [[ $ROTATE_SECRETS -eq 0 ]]; then
        log "[ERROR] Weak or missing secrets detected in existing env file: ${canonical} (${weak_found} weak key(s))"
        log "[ERROR] Refusing to overwrite — existing secrets are preserved."
        log "[ERROR] If you intend to rotate secrets, re-run with --rotate-secrets (this will invalidate existing JWTs/sessions)."
        log "[ERROR]   bash $0 --env-file ${canonical} --rotate-secrets --no-enable   # or with --system/--user"
        return 1
      else
        do_rotate=1
        reason="existing env has ${weak_found} weak key(s) — rotating as requested via --rotate-secrets"
      fi
    else
      # all strong
      if [[ $ROTATE_SECRETS -eq 1 ]]; then
        do_rotate=1
        reason="rotation requested via --rotate-secrets"
      else
        # preserve
        info "Existing env ${canonical} has strong secrets — preserving (no rotation)."
        # ensure canonical not overwritten by source file later
        ENV_FILE="${canonical}"
        ENV_ARG=(--env-file "${canonical}")
        export OAOS_ENV_FILE="${canonical}"
        return 0
      fi
    fi
  fi

  # At this point we need to generate or rotate
  if [[ $do_generate -eq 1 ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      log "[DRY-RUN] Would generate 64-hex secrets for ${canonical} (${reason}) — JWT_SIGNING_KEY, AUDIT_SIGNING_KEY, ADMIN_JWT_SECRET, VAULT/OAOS_ENCRYPTION_KEY (aliases, same value)"
      # For dry-run, also update ENV_FILE to canonical for preflight preview (preflight will still see missing secrets, so we mention)
      info "DRY-RUN: new install would auto-generate secrets (64-hex) — never printed"
      # Don't actually create file; but set ENV_FILE to canonical for subsequent steps? Keep original so preflight warns but we indicate generation
      return 0
    fi
    info "New install: generating secure 64-hex secrets for ${canonical} (${reason}) — never printing values"
    # Build canonical from template or source
    local base_file=""
    if [[ -n "${ENV_FILE}" && -f "${ENV_FILE}" && "${ENV_FILE}" != "${canonical}" ]]; then
      # Use provided source as base (must contain DATABASE_URL etc), then fill secrets
      base_file="${ENV_FILE}"
    else
      # Use example template
      base_file="${REPO_ROOT}/config/oaos.env.example"
      if [[ ! -f "${base_file}" ]]; then base_file="${REPO_ROOT}/deploy/systemd/oaos.env.example"; fi
    fi
    # Generate 4 secrets (encryption single value)
    local s_jwt s_audit s_admin s_enc
    s_jwt=$(generate_hex64); s_audit=$(generate_hex64); s_admin=$(generate_hex64); s_enc=$(generate_hex64)
    # Create canonical via python to preserve non-secret fields and inject secrets
    # For NEW install, only replace weak/missing secrets, preserve strong user-provided values
    python3 - "${base_file}" "${canonical}" "${s_jwt}" "${s_audit}" "${s_admin}" "${s_enc}" <<'PY'
import sys, pathlib, re
base=sys.argv[1]; dest=sys.argv[2]
s_jwt=sys.argv[3]; s_audit=sys.argv[4]; s_admin=sys.argv[5]; s_enc=sys.argv[6]
text=pathlib.Path(base).read_text(encoding="utf-8", errors="ignore")
# Parse existing env to detect weak
env={}
for raw in text.splitlines():
    line=raw.strip()
    if not line or line.startswith("#"): continue
    tmp=raw.strip()
    # strip inline comment outside quotes
    in_s=in_d=False
    cut=None
    for i,ch in enumerate(raw):
        if ch=="'" and not in_d: in_s=not in_s
        elif ch=='"' and not in_s: in_d=not in_d
        elif ch=="#" and not in_s and not in_d:
            cut=i; break
    if cut is not None: raw=raw[:cut]
    raw=raw.strip()
    if raw.startswith("export "): raw=raw[len("export "):].strip()
    if "=" not in raw: continue
    k,v=raw.split("=",1)
    k=k.strip(); v=v.strip()
    if len(v)>=2 and ((v[0]=='"' and v[-1]=='"') or (v[0]=="'" and v[-1]=="'")): v=v[1:-1]
    env[k]=v
def is_weak(v):
    if v is None: return True
    s=v.strip()
    if not s: return True
    low=s.lower()
    if "change_me" in low or "changeme" in low: return True
    if s in ("secret","password","changeme","dev-only-change-me","dev-signing-key-please-change"): return True
    if low.startswith("dev-") and len(s)<32: return True
    if len(s)<32: return True
    return False
# Determine which secrets need generation
need={}
need["JWT_SIGNING_KEY"]= is_weak(env.get("JWT_SIGNING_KEY",""))
need["AUDIT_SIGNING_KEY"]= is_weak(env.get("AUDIT_SIGNING_KEY",""))
need["ADMIN_JWT_SECRET"]= is_weak(env.get("ADMIN_JWT_SECRET",""))
enc_weak = is_weak(env.get("VAULT_ENCRYPTION_KEY","")) and is_weak(env.get("OAOS_ENCRYPTION_KEY",""))
# If encryption has one strong value, reuse that strong value for both instead of generating new
enc_reuse=""
if not enc_weak:
    # keep existing strong value
    if not is_weak(env.get("VAULT_ENCRYPTION_KEY","")):
        enc_reuse=env.get("VAULT_ENCRYPTION_KEY")
    else:
        enc_reuse=env.get("OAOS_ENCRYPTION_KEY")
    need["VAULT_ENCRYPTION_KEY"]= False
    need["OAOS_ENCRYPTION_KEY"]= False
else:
    need["VAULT_ENCRYPTION_KEY"]= True
    need["OAOS_ENCRYPTION_KEY"]= True
# Build keys map: only weak ones get generated value; strong ones keep original (or reuse for enc)
keys={}
if need["JWT_SIGNING_KEY"]: keys["JWT_SIGNING_KEY"]=s_jwt
if need["AUDIT_SIGNING_KEY"]: keys["AUDIT_SIGNING_KEY"]=s_audit
if need["ADMIN_JWT_SECRET"]: keys["ADMIN_JWT_SECRET"]=s_admin
if enc_weak:
    keys["VAULT_ENCRYPTION_KEY"]=s_enc
    keys["OAOS_ENCRYPTION_KEY"]=s_enc
# Also ensure both encryption aliases are set to same value when enc_reuse or enc_weak
# If enc_reuse case, we need to ensure both keys exist with same strong value
if not enc_weak and enc_reuse:
    keys["VAULT_ENCRYPTION_KEY"]=enc_reuse
    keys["OAOS_ENCRYPTION_KEY"]=enc_reuse
    # Only add to output if missing; we will handle found logic below: if key already present strong, keep original, else add reuse
    # For simplicity, we will ensure both keys are present in file with reuse value if not already present
    pass
lines=text.splitlines()
found={k: False for k in set(list(keys.keys()) + ["JWT_SIGNING_KEY","AUDIT_SIGNING_KEY","ADMIN_JWT_SECRET","VAULT_ENCRYPTION_KEY","OAOS_ENCRYPTION_KEY"])}
# Actually we track all canonical keys for found detection
out=[]
for raw in lines:
    stripped=raw.strip()
    if not stripped or stripped.startswith("#"):
        out.append(raw)
        continue
    tmp=raw.strip()
    export_prefix=""
    if tmp.startswith("export "):
        export_prefix="export "
        tmp=tmp[len("export "):].strip()
    matched=None
    for k in ["JWT_SIGNING_KEY","AUDIT_SIGNING_KEY","ADMIN_JWT_SECRET","VAULT_ENCRYPTION_KEY","OAOS_ENCRYPTION_KEY"]:
        if tmp.startswith(k+"="):
            matched=k
            break
    if matched:
        found[matched]=True
        if matched in keys:
            # This key needs injection (weak or missing encryption alias)
            # For enc_reuse case where original strong exists, keys contains reuse value; but if current line is the strong one, we keep it (same value)
            # If current line is weak, replace with generated/reuse
            # Check if original line's value was weak — then replace
            orig_v=env.get(matched,"")
            if is_weak(orig_v):
                out.append(f"{export_prefix}{matched}={keys[matched]}")
            else:
                # preserve strong original (already strong) — but for enc alias, ensure both aliases get reuse if one missing
                if matched in ("VAULT_ENCRYPTION_KEY","OAOS_ENCRYPTION_KEY") and not enc_weak:
                    # ensure value is reuse (strong) — if current value differs from reuse, keep reuse to keep aliases same
                    # Keep original if it is already reuse; else set to reuse
                    if orig_v != enc_reuse:
                        out.append(f"{export_prefix}{matched}={enc_reuse}")
                    else:
                        out.append(raw)
                else:
                    out.append(raw)
        else:
            # strong key not in keys (preserved) — keep original
            out.append(raw)
    else:
        out.append(raw)
# Append missing keys that need injection
for k,v in keys.items():
    if not found[k]:
        out.append(f"{k}={v}")
# For enc_reuse where both aliases need to be present, ensure missing alias added
if not enc_weak:
    for k in ["VAULT_ENCRYPTION_KEY","OAOS_ENCRYPTION_KEY"]:
        if not found[k]:
            out.append(f"{k}={enc_reuse}")
# Ensure OAOS_ENV=production exists (replace if weak)
has_env=False
for i, line in enumerate(out):
    if line.strip().startswith("OAOS_ENV=") or line.strip().startswith("export OAOS_ENV="):
        has_env=True
        # extract value
        val=line.split("=",1)[1].strip().strip('"').strip("'")
        if val.lower() not in ("production","prod"):
            # preserve export prefix if present
            if line.strip().startswith("export "):
                out[i]="export OAOS_ENV=production"
            else:
                out[i]="OAOS_ENV=production"
        break
if not has_env:
    out.append("OAOS_ENV=production")
pathlib.Path(dest).parent.mkdir(parents=True, exist_ok=True)
pathlib.Path(dest).write_text("\n".join(out)+"\n", encoding="utf-8")
PY
    chmod 600 "${canonical}" || true
    if [[ "${MODE}" == "system" ]]; then
      # For system, try to chown root:root if running as root, else keep
      if [[ $EUID -eq 0 ]]; then chown root:root "${canonical}" 2>/dev/null || true; fi
    fi
    info "Generated canonical env file with 64-hex secrets: ${canonical} (0600, never printed)"
    ENV_FILE="${canonical}"
    ENV_ARG=(--env-file "${canonical}")
    export OAOS_ENV_FILE="${canonical}"
    return 0
  fi

  if [[ $do_rotate -eq 1 ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      log "[DRY-RUN] [WARN] Would rotate all canonical secrets in ${canonical} — ${reason} — this will invalidate existing JWTs/sessions and encrypted vault data if re-encrypted"
      return 0
    fi
    log "[WARN] Rotating all canonical secrets in ${canonical} — ${reason} — this will invalidate existing JWTs/sessions!"
    log "[WARN] Ensure you restart services after rotation and re-issue tokens. Vault data encrypted with old key will need re-encryption."
    local s_jwt s_audit s_admin s_enc
    s_jwt=$(generate_hex64); s_audit=$(generate_hex64); s_admin=$(generate_hex64); s_enc=$(generate_hex64)
    python3 - "${canonical}" "${s_jwt}" "${s_audit}" "${s_admin}" "${s_enc}" <<'PY'
import sys, pathlib
path=sys.argv[1]
s_jwt=sys.argv[2]; s_audit=sys.argv[3]; s_admin=sys.argv[4]; s_enc=sys.argv[5]
text=pathlib.Path(path).read_text(encoding="utf-8", errors="ignore")
keys={"JWT_SIGNING_KEY": s_jwt, "AUDIT_SIGNING_KEY": s_audit, "ADMIN_JWT_SECRET": s_admin, "VAULT_ENCRYPTION_KEY": s_enc, "OAOS_ENCRYPTION_KEY": s_enc}
lines=text.splitlines()
found={k: False for k in keys}
out=[]
for raw in lines:
    stripped=raw.strip()
    if not stripped or stripped.startswith("#"):
        out.append(raw)
        continue
    tmp=raw.strip()
    prefix=tmp
    if tmp.startswith("export "):
        prefix=tmp[len("export "):].strip()
    matched=None
    for k in keys:
        if prefix.startswith(k+"="):
            matched=k
            break
    if matched:
        found[matched]=True
        # preserve export prefix if present
        if raw.strip().startswith("export "):
            out.append(f"export {matched}={keys[matched]}")
        else:
            out.append(f"{matched}={keys[matched]}")
    else:
        out.append(raw)
for k,v in keys.items():
    if not found[k]:
        out.append(f"{k}={v}")
pathlib.Path(path).write_text("\n".join(out)+"\n", encoding="utf-8")
PY
    chmod 600 "${canonical}" || true
    info "Rotated secrets in ${canonical} (0600, never printed) — restart services to apply"
    ENV_FILE="${canonical}"
    ENV_ARG=(--env-file "${canonical}")
    export OAOS_ENV_FILE="${canonical}"
    return 0
  fi
}

# Run secret ensure before preflight
if ! ensure_canonical_secrets "${CANONICAL_ENV_FILE}"; then
  exit 1
fi

# If canonical was newly generated or rotated, ENV_FILE already points there
# If dry-run new install, ENV_FILE may still be source with missing secrets — skip strict preflight? We still run preflight but note that dry-run generation would satisfy
# For dry-run new install where canonical missing, we will run preflight on source (which lacks secrets) but we already logged Would generate, so don't fail dry-run
if [[ $DRY_RUN -eq 1 && ! -f "${CANONICAL_ENV_FILE}" ]]; then
  info "DRY-RUN: canonical ${CANONICAL_ENV_FILE} would be generated with 64-hex secrets (preflight on source may show weak secrets — this is expected in dry-run)"
fi

# --- 1. Preflight: friendly config check (no secret output) -
info "Step 1/5: Preflight — check-production-config.sh (no secret output)"
if [[ ! -f "${CHECK_SCRIPT}" ]]; then
  die "Check script not found: ${CHECK_SCRIPT}"
fi
if ! bash "${CHECK_SCRIPT}" "${ENV_ARG[@]}"; then
  if [[ $DRY_RUN -eq 1 ]]; then
    # In dry-run, new install or --rotate-secrets would fix weak secrets — don't abort, just report
    if [[ ! -f "${CANONICAL_ENV_FILE}" ]]; then
      info "DRY-RUN: preflight on source shows weak secrets but canonical would be generated with 64-hex (see Would generate above) — continuing dry-run"
      info "DRY-RUN Preflight would be OK after generation — no secrets printed"
    elif [[ $ROTATE_SECRETS -eq 1 ]]; then
      info "DRY-RUN: preflight shows weak secrets but --rotate-secrets would rotate them — continuing dry-run"
      info "DRY-RUN Preflight would be OK after rotation — no secrets printed"
    else
      echo "" >&2
      die "Preflight failed — fix the [ERROR] lines above before installing.

  Quick fix:
    cp ${REPO_ROOT}/config/oaos.env.example ${REPO_ROOT}/config/oaos.env
    chmod 600 ${REPO_ROOT}/config/oaos.env
    vi ${REPO_ROOT}/config/oaos.env  # replace every CHANGE_ME_* (see header comments)
    # generate strong secrets:
    #   openssl rand -hex 32
  Then re-run:
    bash ${CHECK_SCRIPT} --env-file <your-env-file>
  And re-run this installer.

  Never commit the real env file. For existing installs with weak secrets, use --rotate-secrets to rotate."
    fi
  else
    echo "" >&2
    die "Preflight failed — fix the [ERROR] lines above before installing.

  Quick fix:
    cp ${REPO_ROOT}/config/oaos.env.example ${REPO_ROOT}/config/oaos.env
    chmod 600 ${REPO_ROOT}/config/oaos.env
    vi ${REPO_ROOT}/config/oaos.env  # replace every CHANGE_ME_* (see header comments)
    # generate strong secrets:
    #   openssl rand -hex 32
  Then re-run:
    bash ${CHECK_SCRIPT} --env-file <your-env-file>
  And re-run this installer.

  Never commit the real env file. For existing installs with weak secrets, use --rotate-secrets to rotate."
  fi
else
  info "Preflight OK — no secrets printed, all required keys present and strong."
fi

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
else
  # User units cannot read /etc/oaos by default. Keep the selected env file in
  # the ignored repo-local config path with restrictive permissions.
  USER_ENV_FILE="${REPO_ROOT}/config/oaos.env"
  if [[ "${ENV_FILE}" != "${USER_ENV_FILE}" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      log "[DRY-RUN] Would install env file ${ENV_FILE} -> ${USER_ENV_FILE} (0600, user-owned)"
    else
      install -d -m 0700 "${REPO_ROOT}/config"
      install -m 0600 "${ENV_FILE}" "${USER_ENV_FILE}"
      info "Installed canonical user-systemd env file: ${USER_ENV_FILE}"
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
