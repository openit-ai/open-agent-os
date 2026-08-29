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
import os
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import HTTPException

try:
    from jose import jwt, JWTError, ExpiredSignatureError  # type: ignore
except Exception:  # pragma: no cover
    jwt = None  # type: ignore
    JWTError = Exception  # type: ignore
    ExpiredSignatureError = Exception  # type: ignore

logger = logging.getLogger(__name__)

_DEV_SIGNING_KEY = "dev-signing-key-please-change"
ISSUER = "control-plane"
AUDIENCE = "execution-gateway"

def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        if os.getenv(k, "").strip().lower() in ("production", "prod"):
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
    if os.getenv("OAOS_ENFORCE_SIGNED_CONTEXT", "").lower() in ("1", "true", "yes", "on"):
        return False
    if os.getenv("OAOS_ENFORCE_SIGNED_CONTEXT_STRICT", "").lower() in ("1", "true", "yes", "on"):
        return False
    return True

def _fail_open_telemetry(reason: str, **fields):
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    msg = f"[fail-open] component=execution-gateway reason={reason} {extra}".strip()
    logger.warning(msg)
    try:
        import sys
        print(msg, file=sys.stderr)
    except Exception:
        pass

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
) -> str:
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
    if delegation_id:
        payload["delegation_id"] = delegation_id
    if credential_binding_id:
        payload["credential_binding_id"] = credential_binding_id
    return jwt.encode(payload, key, algorithm="HS256")  # type: ignore

def verify_agent_context_jwt(token: str) -> dict:
    if not token:
        raise HTTPException(status_code=401, detail="missing X-Agent-Context-JWT")
    if jwt is None:
        raise HTTPException(status_code=500, detail="jwt library unavailable")
    key = get_signing_key()
    try:
        claims = jwt.decode(token, key, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER)
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="CONTEXT_EXPIRED")
    except JWTError as e:
        msg = str(e).lower()
        if "audience" in msg or "aud" in msg:
            raise HTTPException(status_code=401, detail=f"INVALID_CONTEXT: invalid audience: {e}")
        if "issuer" in msg or "iss" in msg:
            raise HTTPException(status_code=401, detail=f"INVALID_CONTEXT: invalid issuer: {e}")
        raise HTTPException(status_code=401, detail=f"INVALID_CONTEXT: invalid token: {e}")
    except RuntimeError:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"INVALID_CONTEXT: {e}")
    for field in ("tenant_id", "user_id", "agent_id", "session_id"):
        if not claims.get(field):
            raise HTTPException(status_code=401, detail=f"INVALID_CONTEXT: missing {field}")
    if ":" not in str(claims.get("user_id", "")):
        raise HTTPException(status_code=401, detail="INVALID_CONTEXT: user_id must be namespaced")
    return claims

def parse_and_verify_context(
    x_agent_context_jwt: Optional[str],
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
        claims = verify_agent_context_jwt(x_agent_context_jwt.strip())
        ctx = dict(claims)
        if x_tenant_id and x_tenant_id != ctx.get("tenant_id"):
            raise HTTPException(status_code=403, detail="TENANT_MISMATCH: header tenant != context tenant")
        if x_user_id and x_user_id != ctx.get("user_id"):
            raise HTTPException(status_code=401, detail="INVALID_CONTEXT: X-User-Id != JWT user_id")
        if x_agent_id and x_agent_id != ctx.get("agent_id"):
            raise HTTPException(status_code=401, detail="INVALID_CONTEXT: X-Agent-Id != JWT agent_id")
        if x_session_id and x_session_id != ctx.get("session_id"):
            raise HTTPException(status_code=401, detail="INVALID_CONTEXT: X-Session-Id != JWT session_id")
        if x_delegation_id and x_delegation_id != ctx.get("delegation_id"):
            raise HTTPException(status_code=401, detail="INVALID_CONTEXT: delegation mismatch")
        return ctx
    if _allow_plaintext():
        _fail_open_telemetry("plaintext_context_fallback", has_context=bool(x_agent_context))
        import json, base64
        ctx: dict = {}
        if x_agent_context:
            raw = x_agent_context.strip()
            decoded = None
            try:
                decoded = json.loads(raw)
            except Exception:
                try:
                    padded = raw + "=" * (-len(raw) % 4)
                    decoded = json.loads(base64.b64decode(padded).decode("utf-8"))
                except Exception:
                    raise HTTPException(status_code=400, detail="invalid X-Agent-Context header: not valid JSON nor base64 JSON")
            if isinstance(decoded, dict):
                ctx.update(decoded)
        if x_tenant_id:
            ctx["tenant_id"] = x_tenant_id
        if x_user_id:
            ctx["user_id"] = x_user_id
        if x_agent_id:
            ctx["agent_id"] = x_agent_id
        if x_session_id:
            ctx["session_id"] = x_session_id
        if x_trace_id:
            ctx["trace_id"] = x_trace_id
        if x_request_id:
            ctx["request_id"] = x_request_id
        if x_delegation_id:
            ctx["delegation_id"] = x_delegation_id
        if x_credential_binding_id:
            ctx["credential_binding_id"] = x_credential_binding_id
        return ctx
    raise HTTPException(status_code=401, detail="INVALID_CONTEXT: missing or invalid X-Agent-Context-JWT")
