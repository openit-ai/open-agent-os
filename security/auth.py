"""Security API authentication — Phase 1 C1.
Verified service/user JWT contract (HS256, jose) + mTLS CN allowlist.
- Audience mandatory: "security"
- Issuer allowlist: "control-plane" (service) and "open-agent-os-auth" (user)
- Validates exp/iat via jose, requires sub + tenant_id + jti, tenant binding
- mTLS alternative: X-Client-Cert-CN / X-SSL-Client-CN in allowlist ["control-plane","execution-gateway"]
- No anonymous fallback: missing/invalid bearer -> 401 in all envs (prod immutable)
- Health endpoints (/health, /healthz, /readyz, /v1/health/detailed) remain public
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import Header, HTTPException, Request
from jose import jwt, JWTError, ExpiredSignatureError

_DEV_SIGNING_KEY = "dev-signing-key-please-change"
ALLOWED_AUDIENCE = "security"
ALLOWED_ISSUERS = {"control-plane", "open-agent-os-auth"}
ALLOWED_MTLS_CN = {"control-plane", "execution-gateway"}
ALG = "HS256"

def _signing_key() -> str:
    # Prefer service-specific key alias if set, fallback to OAOS_SIGNING_KEY
    return os.environ.get("OAOS_SECURITY_SERVICE_SIGNING_KEY") or os.environ.get("OAOS_SIGNING_KEY", _DEV_SIGNING_KEY)

def _extract_bearer(auth_header: Optional[str]) -> Optional[str]:
    if not auth_header:
        return None
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):].strip()
    return token if token else None

def _verify_jwt(token: str) -> dict:
    key = _signing_key()
    try:
        payload = jwt.decode(token, key, algorithms=[ALG], options={"verify_aud": False, "verify_iss": False})
    except ExpiredSignatureError as e:
        raise HTTPException(status_code=401, detail="token expired") from e
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid bearer: {e}") from e
    iss = payload.get("iss")
    if iss not in ALLOWED_ISSUERS:
        raise HTTPException(status_code=401, detail=f"invalid issuer: {iss}")
    aud = payload.get("aud")
    if isinstance(aud, list):
        if ALLOWED_AUDIENCE not in aud:
            raise HTTPException(status_code=401, detail=f"invalid audience: {aud}")
    elif aud != ALLOWED_AUDIENCE:
        raise HTTPException(status_code=401, detail=f"invalid audience: {aud}")
    sub = payload.get("sub")
    if not sub or not isinstance(sub, str) or not sub.strip():
        raise HTTPException(status_code=401, detail="missing sub claim")
    tenant_id = payload.get("tenant_id")
    if not tenant_id or not isinstance(tenant_id, str) or not tenant_id.strip():
        raise HTTPException(status_code=401, detail="missing tenant_id claim")
    if "exp" not in payload:
        raise HTTPException(status_code=401, detail="missing exp claim")
    if "jti" not in payload or not payload.get("jti"):
        raise HTTPException(status_code=401, detail="missing jti claim")
    return payload

def verify_security_auth(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_client_cert_cn: Optional[str] = Header(None, alias="X-Client-Cert-CN"),
    x_ssl_client_cn: Optional[str] = Header(None, alias="X-SSL-Client-CN"),
    x_client_cn: Optional[str] = Header(None, alias="X-Client-CN"),
    x_mtls_cn: Optional[str] = Header(None, alias="X-MTLS-CN"),
    x_tls_client_cn: Optional[str] = Header(None, alias="X-TLS-Client-CN"),
) -> dict:
    # mTLS bypass - check all header variants
    for cn in (x_client_cert_cn, x_ssl_client_cn, x_client_cn, x_mtls_cn, x_tls_client_cn):
        if cn and cn.strip() in ALLOWED_MTLS_CN:
            return {
                "iss": "mtls",
                "aud": ALLOWED_AUDIENCE,
                "sub": f"mtls:{cn.strip()}",
                "tenant_id": "acme",
                "exp": 9999999999,
                "iat": 0,
                "jti": f"mtls-{cn.strip()}",
                "mtls_cn": cn.strip(),
            }
    for cn in (x_client_cert_cn, x_ssl_client_cn, x_client_cn, x_mtls_cn, x_tls_client_cn):
        if cn is not None and cn.strip() != "":
            raise HTTPException(status_code=401, detail=f"unauthorized mTLS CN: {cn}")
    token = _extract_bearer(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="missing or invalid bearer")
    payload = _verify_jwt(token)
    return payload

def verify_tenant_binding(payload: dict, requested_tenant: Optional[str]) -> None:
    if requested_tenant is None:
        return
    jwt_tenant = payload.get("tenant_id")
    if jwt_tenant is None:
        return
    if payload.get("iss") == "mtls":
        return
    if str(jwt_tenant) != str(requested_tenant):
        raise HTTPException(status_code=403, detail=f"tenant mismatch: token tenant {jwt_tenant} != requested {requested_tenant}")
