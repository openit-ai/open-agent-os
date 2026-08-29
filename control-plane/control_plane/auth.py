"""CP Identity verification — H1.

JWT: HS256 with explicit env-configured key/issuer/audience.
- Key priority: OAOS_USER_JWT_SIGNING_KEY > OAOS_JWT_SIGNING_KEY > OAOS_SIGNING_KEY > ADMIN_JWT_SECRET
- Issuer: OAOS_USER_JWT_ISSUER > OAOS_JWT_ISSUER > OAOS_AUTH_ISSUER (default open-agent-os-auth)
- Audience: OAOS_USER_JWT_AUDIENCE > OAOS_JWT_AUDIENCE > OAOS_AUTH_AUDIENCE (default control-plane)
"""
import os, logging
from typing import Optional
from fastapi import HTTPException
try:
    from jose import jwt, JWTError, ExpiredSignatureError  # type: ignore
except Exception:
    jwt = None  # type: ignore
    JWTError = Exception  # type: ignore
    ExpiredSignatureError = Exception  # type: ignore
logger = logging.getLogger(__name__)
_DEV_SIGNING_KEY = "dev-signing-key-please-change"
_DEFAULT_ISSUER = "open-agent-os-auth"
_DEFAULT_AUDIENCE = "control-plane"

def is_production() -> bool:
    for k in ("OAOS_ENV","ENV","OAOS_ENVIRONMENT","APP_ENV","ENVIRONMENT"):
        if os.getenv(k,"").strip().lower() in ("production","prod"): return True
    return False

def get_signing_key() -> str:
    for ek in ("OAOS_USER_JWT_SIGNING_KEY","OAOS_JWT_SIGNING_KEY","OAOS_SIGNING_KEY","ADMIN_JWT_SECRET"):
        v=os.getenv(ek)
        if v and v.strip(): return v.strip()
    if is_production(): raise RuntimeError("JWT signing key must be set (OAOS_USER_JWT_SIGNING_KEY / OAOS_JWT_SIGNING_KEY / OAOS_SIGNING_KEY)")
    return _DEV_SIGNING_KEY

def get_expected_issuer() -> str:
    for ek in ("OAOS_USER_JWT_ISSUER","OAOS_JWT_ISSUER","OAOS_AUTH_ISSUER","OAOS_JWT_ISS","OAOS_ISSUER"):
        v=os.getenv(ek)
        if v and v.strip(): return v.strip()
    return _DEFAULT_ISSUER

def get_expected_audience() -> str:
    for ek in ("OAOS_USER_JWT_AUDIENCE","OAOS_JWT_AUDIENCE","OAOS_AUTH_AUDIENCE","OAOS_JWT_AUD","OAOS_AUDIENCE"):
        v=os.getenv(ek)
        if v and v.strip(): return v.strip()
    return _DEFAULT_AUDIENCE

EXPECTED_ISSUER = os.getenv("OAOS_USER_JWT_ISSUER") or os.getenv("OAOS_JWT_ISSUER") or _DEFAULT_ISSUER
EXPECTED_AUDIENCE = os.getenv("OAOS_USER_JWT_AUDIENCE") or os.getenv("OAOS_JWT_AUDIENCE") or _DEFAULT_AUDIENCE

def _fail_open_telemetry(reason: str, **fields) -> None:
    extra=" ".join(f"{k}={v}" for k,v in fields.items())
    msg=f"[fail-open] component=control-plane reason={reason} {extra}".strip()
    logger.warning(msg)
    try:
        import sys; print(msg,file=sys.stderr)
    except Exception: pass

def _is_test_plaintext_allowed() -> bool:
    if is_production(): return False
    if os.getenv("OAOS_TEST_ALLOW_PLAINTEXT","").lower() in ("1","true","yes","on"): return True
    if os.getenv("OAOS_CP_TEST_ALLOW_PLAINTEXT","").lower() in ("1","true","yes","on"): return True
    if "PYTEST_CURRENT_TEST" in os.environ: return True
    if os.getenv("PYTEST_RUN","").lower() in ("1","true"): return True
    return False

def verify_user_jwt(token: str) -> dict:
    if not token: raise HTTPException(status_code=401, detail="missing bearer token")
    if jwt is None: raise HTTPException(status_code=500, detail="jwt library unavailable")
    key=get_signing_key()
    issuer=get_expected_issuer()
    audience=get_expected_audience()
    try:
        claims=jwt.decode(token,key,algorithms=["HS256"],audience=audience,issuer=issuer)
    except ExpiredSignatureError: raise HTTPException(status_code=401, detail="TOKEN_EXPIRED")
    except JWTError as e:
        msg=str(e).lower()
        if "audience" in msg or "aud" in msg: raise HTTPException(status_code=401, detail=f"invalid audience: {e}")
        if "issuer" in msg or "iss" in msg: raise HTTPException(status_code=401, detail=f"invalid issuer: {e}")
        if "expired" in msg: raise HTTPException(status_code=401, detail="TOKEN_EXPIRED")
        raise HTTPException(status_code=401, detail=f"invalid token: {e}")
    except RuntimeError: raise
    except Exception as e: raise HTTPException(status_code=401, detail=f"invalid token: {e}")
    for f in ("sub","tenant_id","exp"):
        if not claims.get(f): raise HTTPException(status_code=401, detail=f"missing claim: {f}")
    return claims

def resolve_caller_user(authorization: Optional[str], x_user_id: Optional[str], body_user_id: Optional[str]=None, body_tenant_id: Optional[str]=None) -> str:
    token=None
    if authorization:
        auth=authorization.strip()
        if auth.lower().startswith("bearer "): token=auth[7:].strip()
        elif auth: raise HTTPException(status_code=401, detail="missing or invalid bearer: expected 'Bearer <token>'")
    if token:
        claims=verify_user_jwt(token)
        sub=str(claims.get("sub"))
        if body_tenant_id and str(claims.get("tenant_id"))!=str(body_tenant_id): raise HTTPException(status_code=401, detail="TENANT_MISMATCH: token tenant != body tenant")
        if x_user_id and x_user_id!=sub: raise HTTPException(status_code=401, detail="IDENTITY_MISMATCH: X-User-Id != token.sub")
        if body_user_id and body_user_id!=sub: raise HTTPException(status_code=401, detail="IDENTITY_MISMATCH: body user_id != token.sub")
        if not claims.get("tenant_id"): raise HTTPException(status_code=401, detail="missing tenant_id in token")
        return sub
    if is_production(): raise HTTPException(status_code=401, detail="missing or invalid bearer: JWT required in production")
    if _is_test_plaintext_allowed():
        if x_user_id:
            _fail_open_telemetry("plaintext_identity_fallback",user=x_user_id)
            if body_user_id and body_user_id!=x_user_id: raise HTTPException(status_code=401, detail="IDENTITY_MISMATCH: X-User-Id != body user_id")
            return x_user_id
        if body_user_id:
            _fail_open_telemetry("plaintext_identity_fallback_body",user=body_user_id)
            return body_user_id
        raise HTTPException(status_code=401, detail="X-User-Id required (employee:...)")
    raise HTTPException(status_code=401, detail="missing or invalid bearer: JWT required")

def issue_user_jwt(sub: str, tenant_id: str, ttl_seconds: int=3600, signing_key: str|None=None, extra: dict|None=None, issuer: str|None=None, audience: str|None=None) -> str:
    if jwt is None: raise RuntimeError("jwt unavailable")
    import time, uuid
    key=signing_key or get_signing_key()
    iss=issuer or get_expected_issuer()
    aud=audience or get_expected_audience()
    now=int(time.time())
    payload={"iss":iss,"aud":aud,"sub":sub,"tenant_id":tenant_id,"exp":now+ttl_seconds,"iat":now,"jti":uuid.uuid4().hex}
    if extra: payload.update(extra)
    return jwt.encode(payload,key,algorithm="HS256")
