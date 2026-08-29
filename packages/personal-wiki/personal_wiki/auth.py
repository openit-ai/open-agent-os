"""Personal Wiki JWT verifier — H3 verified owner resolution.

- Verified JWT only (HS256, exp/iss/aud/tenant/agent/scope mandatory)
- Issuer allowlist: control-plane, security, open-agent-os-auth
- Audience allowlist: wiki-fs, memory-service, wiki, security
- Scope: wiki:read | wiki:write (write operation requires wiki:write; read accepts both)
- Tenant/agent binding: JWT tenant/agent must match requested path tenant/agent
- Path traversal: vault path must remain within vault_root/tenant/agent
- Production fail-closed: no unverified claims, no header fallback
- Non-prod explicit fixture only: X-User-Id fallback allowed ONLY when OAOS_ENV != production and (PYTEST_CURRENT_TEST set or OAOS_ALLOW_TEST_FIXTURE=1)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from jose import jwt, JWTError, ExpiredSignatureError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ALLOWED_ISSUERS = {"control-plane", "security", "open-agent-os-auth"}
ALLOWED_AUDIENCES = {"wiki-fs", "memory-service", "wiki", "security"}
ALLOWED_SCOPES = {"wiki:read", "wiki:write"}
ALG = "HS256"
_DEV_KEY_SENTINEL = "dev-admin-jwt-secret-please-change"

# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        v = os.getenv(k, "").strip().lower()
        if v in ("production", "prod"):
            return True
    return False

def _allow_test_fixture() -> bool:
    """Explicit non-prod test fixture only.

    True when NOT production and (PYTEST_CURRENT_TEST set or OAOS_ALLOW_TEST_FIXTURE truthy).
    This is the ONLY path that allows X-User-Id header fallback.
    """
    if _is_production():
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    flag = os.environ.get("OAOS_ALLOW_TEST_FIXTURE", "") or os.environ.get("OAOS_ALLOW_TEST_FALLBACK", "")
    if flag.strip().lower() in ("1", "true", "yes", "on"):
        return True
    # Also allow when OAOS_ENV explicitly test/development without production
    # but still require explicit fixture marker: we treat PYTEST as marker, above.
    # For backwards compat with existing tests that run under pytest without explicit flag, PYTEST path suffices.
    return False

def _wiki_signing_key() -> str:
    # Priority: OAOS_SIGNING_KEY > OAOS_SECURITY_SERVICE_SIGNING_KEY > JWT_SIGNING_KEY > ADMIN_JWT_SECRET
    for k in ("OAOS_SIGNING_KEY", "OAOS_SECURITY_SERVICE_SIGNING_KEY", "JWT_SIGNING_KEY", "ADMIN_JWT_SECRET", "OAOS_JWT_SIGNING_KEY"):
        v = os.getenv(k, "")
        if v and v.strip():
            return v.strip()
    return _DEV_KEY_SENTINEL

# ---------------------------------------------------------------------------
# JWT verification
# ---------------------------------------------------------------------------

def verify_wiki_jwt(token: str, required_scope: Optional[str] = None) -> dict:
    """Verify Wiki JWT with issuer/audience/exp/tenant/agent/scope checks.

    required_scope: "wiki:read" or "wiki:write". If "wiki:read", both wiki:read and wiki:write are accepted. If "wiki:write", only wiki:write accepted.
    Raises HTTPException 401 on failure.
    """
    if not token or not token.strip():
        raise HTTPException(status_code=401, detail="missing wiki JWT")
    key = _wiki_signing_key()
    # In production, dev key must not be used
    if _is_production() and key == _DEV_KEY_SENTINEL:
        raise HTTPException(status_code=503, detail="wiki JWT signing key not configured in production")
    try:
        # Use manual aud/iss checks to allow custom error messages; disable auto verify_iss/aud but verify exp/jti via decode
        payload = jwt.decode(token, key, algorithms=[ALG], options={"verify_aud": False, "verify_iss": False})
    except ExpiredSignatureError as e:
        raise HTTPException(status_code=401, detail="wiki JWT expired") from e
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid wiki JWT: {e}") from e

    iss = payload.get("iss")
    if not iss or iss not in ALLOWED_ISSUERS:
        raise HTTPException(status_code=401, detail=f"invalid issuer: {iss}")
    aud = payload.get("aud")
    # aud can be string or list
    aud_ok = False
    if isinstance(aud, list):
        aud_ok = any(a in ALLOWED_AUDIENCES for a in aud)
    elif isinstance(aud, str):
        aud_ok = aud in ALLOWED_AUDIENCES
    if not aud_ok:
        raise HTTPException(status_code=401, detail=f"invalid audience: {aud}")
    sub = payload.get("sub")
    if not sub or not isinstance(sub, str) or not sub.strip():
        raise HTTPException(status_code=401, detail="missing sub claim")
    tenant_id = payload.get("tenant_id")
    if not tenant_id or not isinstance(tenant_id, str) or not tenant_id.strip():
        raise HTTPException(status_code=401, detail="missing tenant_id claim")
    agent_id = payload.get("agent_id")
    if not agent_id or not isinstance(agent_id, str) or not agent_id.strip():
        raise HTTPException(status_code=401, detail="missing agent_id claim")
    scope = payload.get("scope")
    if not scope or not isinstance(scope, str) or scope.strip() not in ALLOWED_SCOPES:
        raise HTTPException(status_code=401, detail=f"missing or invalid scope: {scope}")
    if "exp" not in payload:
        raise HTTPException(status_code=401, detail="missing exp claim")
    if "jti" not in payload or not payload.get("jti"):
        raise HTTPException(status_code=401, detail="missing jti claim")

    # Scope enforcement
    if required_scope:
        # Normalize: wiki:read allows wiki:read or wiki:write; wiki:write requires wiki:write only
        if required_scope == "wiki:read":
            if scope not in ("wiki:read", "wiki:write"):
                raise HTTPException(status_code=401, detail=f"invalid scope for read: {scope}")
        elif required_scope == "wiki:write":
            if scope != "wiki:write":
                raise HTTPException(status_code=401, detail=f"scope {scope} not authorized for write (requires wiki:write)")
        else:
            if scope != required_scope:
                raise HTTPException(status_code=401, detail=f"scope mismatch: {scope} != {required_scope}")

    return payload

def verify_tenant_agent_binding(payload: dict, requested_tenant: Optional[str], requested_agent: Optional[str]) -> None:
    """Ensure JWT tenant/agent matches requested resource tenant/agent.

    Raises 403 on mismatch.
    """
    if requested_tenant is not None:
        jwt_tenant = payload.get("tenant_id")
        if jwt_tenant is None:
            raise HTTPException(status_code=403, detail="missing tenant_id in JWT")
        if str(jwt_tenant) != str(requested_tenant):
            raise HTTPException(status_code=403, detail=f"tenant mismatch: token tenant {jwt_tenant} != requested {requested_tenant}")
    if requested_agent is not None:
        jwt_agent = payload.get("agent_id")
        if jwt_agent is None:
            raise HTTPException(status_code=403, detail="missing agent_id in JWT")
        if str(jwt_agent) != str(requested_agent):
            raise HTTPException(status_code=403, detail=f"agent mismatch: token agent {jwt_agent} != requested {requested_agent}")

# ---------------------------------------------------------------------------
# Path traversal guard
# ---------------------------------------------------------------------------

def assert_vault_path_safe(resolved: Path, vault_base: Path) -> None:
    """Ensure resolved path is within vault_base. Raises 403 PATH_TRAVERSAL on escape."""
    try:
        # Resolve both without requiring existence (strict=False)
        base = vault_base.resolve()
        target = resolved.resolve()
    except Exception:
        # Fallback to absolute without resolve
        base = vault_base.absolute()
        target = resolved.absolute()
    # Also handle symlink-less check via relative_to
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=403, detail=f"PATH_TRAVERSAL: path {resolved} escapes vault {vault_base}")

def safe_join_vault(base: Path, *parts: str) -> Path:
    """Join parts under base and ensure no traversal. Raises 403 on traversal."""
    # Reject absolute parts and .. segments early
    for p in parts:
        if not p:
            continue
        # Check for traversal segments before join
        segs = Path(p).parts
        for seg in segs:
            if seg == "..":
                raise HTTPException(status_code=403, detail=f"PATH_TRAVERSAL: '..' in path segment {p}")
        # Also reject absolute paths
        if Path(p).is_absolute():
            raise HTTPException(status_code=403, detail=f"PATH_TRAVERSAL: absolute path {p}")
        # Reject path that after normalization contains ..
        # Use string check for encoded traversal attempts
        if ".." in p.split("/"):
            raise HTTPException(status_code=403, detail=f"PATH_TRAVERSAL: '..' in {p}")
    joined = base.joinpath(*parts)
    assert_vault_path_safe(joined, base)
    return joined
