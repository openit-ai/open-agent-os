"""Execution Gateway Signed Context — H2 (Phase 1).

Invariants:
- I-H2-1: prod only trusts X-Agent-Context-JWT signed by CP (HS256 OAOS_SIGNING_KEY).
          Plaintext X-Agent-Context / X-* headers -> 401 INVALID_CONTEXT in production.
- I-H2-2: non-prod allows plaintext with telemetry for existing-test compat, but if
          OAOS_ENFORCE_SIGNED_CONTEXT=1, even non-prod rejects plaintext.
- I-H2-3: JWT contains tenant_id, user_id, agent_id, session_id, trace_id, request_id,
          verified with iss=control-plane, aud=execution-gateway, exp.
"""
from __future__ import annotations
import base64
import json
import os
import sys
import logging
from typing import Optional

from fastapi import HTTPException

try:
    from jose import jwt, ExpiredSignatureError, JWTError  # type: ignore
except Exception:  # pragma: no cover
    jwt = None  # type: ignore
    ExpiredSignatureError = Exception  # type: ignore
    JWTError = Exception  # type: ignore

logger = logging.getLogger("execution-gateway.auth")

_DEV_SIGNING_KEY = "dev-signing-key-please-change"
ISSUER = "control-plane"
AUDIENCE = "execution-gateway"

def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        v = os.getenv(k, "")
        if v and v.strip().lower() in ("production", "prod"):
            return True
    return False

def get_signing_key() -> str:
    key = os.getenv("OAOS_SIGNING_KEY", _DEV_SIGNING_KEY)
    if _is_production() and key == _DEV_SIGNING_KEY:
        raise RuntimeError("OAOS_SIGNING_KEY must be set to a strong value when OAOS_ENV=production (fail-closed)")
    return key

def _allow_plaintext() -> bool:
    if _is_production():
        return False
    for k in ("OAOS_ENFORCE_SIGNED_CONTEXT", "OAOS_ENFORCE_SIGNED_CONTEXT_STRICT"):
        v = os.getenv(k, "").strip().lower()
        if v in ("1", "true", "yes", "on"):
            return False
    return True

def _fail_open_telemetry(reason: str, **fields) -> None:
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    msg = f"[fail-open] component=execution-gateway reason={reason} {extra}".strip()
    try:
        logger.warning(msg)
    except Exception:
        pass
    print(msg, file=sys.stderr)

def issue_agent_context_jwt(
    tenant_id: str,
    user_id: str,
    agent_id: str,
    session_id: str,
    trace_id: Optional[str] = None,
    request_id: Optional[str] = None,
    security_domain: str = "general",
    ttl_seconds: int = 600,
    signing_key: Optional[str] = None,
    **extra,
) -> str:
    import uuid
    from datetime import datetime, timedelta, timezone
    if jwt is None:
        raise RuntimeError("jwt library unavailable")
    key = signing_key or get_signing_key()
    now = datetime.now(timezone.utc)
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
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
    payload.update({k: v for k, v in extra.items() if v is not None})
    return jwt.encode(payload, key, algorithm="HS256")

def verify_agent_context_jwt(token: str) -> dict:
    if not token:
        raise HTTPException(status_code=401, detail="missing X-Agent-Context-JWT")
    if jwt is None:
        raise HTTPException(status_code=500, detail="jwt library unavailable")
    key = get_signing_key()
    try:
        payload = jwt.decode(token, key, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER)
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="CONTEXT_EXPIRED")
    except JWTError as e:
        msg = str(e).lower()
        if "audience" in msg:
            raise HTTPException(status_code=401, detail=f"INVALID_CONTEXT: invalid audience: {e}")
        if "issuer" in msg:
            raise HTTPException(status_code=401, detail=f"INVALID_CONTEXT: invalid issuer: {e}")
        raise HTTPException(status_code=401, detail=f"INVALID_CONTEXT: invalid token: {e}")
    for field in ("tenant_id", "user_id", "agent_id", "session_id"):
        if not payload.get(field):
            raise HTTPException(status_code=401, detail=f"INVALID_CONTEXT: missing {field}")
    if ":" not in payload.get("user_id", ""):
        raise HTTPException(status_code=401, detail="INVALID_CONTEXT: user_id must be namespaced")
    return payload

def parse_and_verify_context(
    x_agent_context_jwt: Optional[str] = None,
    x_agent_context: Optional[str] = None,
    x_tenant_id: Optional[str] = None,
    x_user_id: Optional[str] = None,
    x_agent_id: Optional[str] = None,
    x_session_id: Optional[str] = None,
    x_trace_id: Optional[str] = None,
    x_request_id: Optional[str] = None,
    x_delegation_id: Optional[str] = None,
    x_credential_binding_id: Optional[str] = None,
) -> dict:
    if x_agent_context_jwt:
        ctx = verify_agent_context_jwt(x_agent_context_jwt)
        if x_tenant_id is not None and x_tenant_id != ctx.get("tenant_id"):
            raise HTTPException(status_code=403, detail="TENANT_MISMATCH: header tenant != context tenant")
        if x_user_id is not None and x_user_id != ctx.get("user_id"):
            raise HTTPException(status_code=401, detail="INVALID_CONTEXT: X-User-Id != JWT user_id")
        if x_agent_id is not None and x_agent_id != ctx.get("agent_id"):
            raise HTTPException(status_code=401, detail="INVALID_CONTEXT: X-Agent-Id != JWT agent_id")
        if x_session_id is not None and x_session_id != ctx.get("session_id"):
            raise HTTPException(status_code=401, detail="INVALID_CONTEXT: X-Session-Id != JWT session_id")
        if x_delegation_id is not None and x_delegation_id != ctx.get("delegation_id"):
            raise HTTPException(status_code=401, detail="INVALID_CONTEXT: delegation mismatch")
        return ctx
    # no JWT
    if _allow_plaintext():
        _fail_open_telemetry("plaintext_context_fallback", has_context=bool(x_agent_context))
        ctx: dict = {}
        if x_agent_context:
            raw = x_agent_context.strip()
            try:
                ctx = json.loads(raw)
            except Exception:
                try:
                    padded = raw + "=" * (-len(raw) % 4)
                    ctx = json.loads(base64.urlsafe_b64decode(padded).decode())
                except Exception:
                    raise HTTPException(status_code=400, detail="invalid X-Agent-Context header: not valid JSON nor base64 JSON")
        # merge X-* headers
        for k, v in {
            "tenant_id": x_tenant_id,
            "user_id": x_user_id,
            "agent_id": x_agent_id,
            "session_id": x_session_id,
            "trace_id": x_trace_id,
            "request_id": x_request_id,
            "delegation_id": x_delegation_id,
            "credential_binding_id": x_credential_binding_id,
        }.items():
            if v is not None:
                ctx[k] = v
        return ctx
    raise HTTPException(status_code=401, detail="INVALID_CONTEXT: missing or invalid X-Agent-Context-JWT")
