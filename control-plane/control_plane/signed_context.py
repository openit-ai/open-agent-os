"""CP Signed AgentContext — H2 issuance (CP -> EGW).

Issues JWT with iss=control-plane, aud=execution-gateway, binding tenant/user/agent/session.
Env-configured: OAOS_SIGNING_KEY (fallback OAOS_AGENT_CONTEXT_SIGNING_KEY), issuer/audience via OAOS_AGENT_CONTEXT_ISSUER/AUDIENCE.
"""
from __future__ import annotations
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    from jose import jwt  # type: ignore
except Exception:  # pragma: no cover
    jwt = None  # type: ignore

_DEV_SIGNING_KEY = "dev-signing-key-please-change"
_DEFAULT_ISSUER = "control-plane"
_DEFAULT_AUDIENCE = "execution-gateway"


def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        if os.getenv(k, "").strip().lower() in ("production", "prod"):
            return True
    return False


def get_signing_key() -> str:
    for ek in ("OAOS_AGENT_CONTEXT_SIGNING_KEY", "OAOS_SIGNING_KEY", "OAOS_JWT_SIGNING_KEY"):
        v = os.getenv(ek)
        if v and v.strip():
            return v.strip()
    if _is_production():
        raise RuntimeError("OAOS_SIGNING_KEY must be set to a strong value when OAOS_ENV=production (fail-closed)")
    return _DEV_SIGNING_KEY


def get_issuer() -> str:
    for ek in ("OAOS_AGENT_CONTEXT_ISSUER", "OAOS_SIGNED_CONTEXT_ISSUER", "OAOS_AGENT_JWT_ISSUER"):
        v = os.getenv(ek)
        if v and v.strip():
            return v.strip()
    return _DEFAULT_ISSUER


def get_audience() -> str:
    for ek in ("OAOS_AGENT_CONTEXT_AUDIENCE", "OAOS_SIGNED_CONTEXT_AUDIENCE", "OAOS_AGENT_JWT_AUDIENCE"):
        v = os.getenv(ek)
        if v and v.strip():
            return v.strip()
    return _DEFAULT_AUDIENCE


ISSUER = os.getenv("OAOS_AGENT_CONTEXT_ISSUER") or os.getenv("OAOS_SIGNED_CONTEXT_ISSUER") or _DEFAULT_ISSUER
AUDIENCE = os.getenv("OAOS_AGENT_CONTEXT_AUDIENCE") or os.getenv("OAOS_SIGNED_CONTEXT_AUDIENCE") or _DEFAULT_AUDIENCE


def issue_agent_context_jwt(
    tenant_id: str,
    user_id: str,
    agent_id: str,
    session_id: str,
    trace_id: Optional[str] = None,
    request_id: Optional[str] = None,
    security_domain: str = "general",
    delegation_id: Optional[str] = None,
    credential_binding_id: Optional[str] = None,
    ttl_seconds: int = 600,
    signing_key: Optional[str] = None,
    issuer: Optional[str] = None,
    audience: Optional[str] = None,
) -> str:
    if jwt is None:
        raise RuntimeError("jwt library unavailable")
    key = signing_key or get_signing_key()
    iss = issuer or get_issuer()
    aud = audience or get_audience()
    now = datetime.now(timezone.utc)
    payload = {
        "iss": iss,
        "aud": aud,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "trace_id": trace_id or f"trace_{uuid.uuid4().hex[:12]}",
        "request_id": request_id or f"req_{uuid.uuid4().hex[:8]}",
        "security_domain": security_domain,
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "iat": int(now.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if delegation_id:
        payload["delegation_id"] = delegation_id
    if credential_binding_id:
        payload["credential_binding_id"] = credential_binding_id
    return jwt.encode(payload, key, algorithm="HS256")  # type: ignore
