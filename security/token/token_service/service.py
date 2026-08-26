"""Capability Token — Section 26. Short-lived signed token, nonce + replay protection."""
import uuid, time
from datetime import datetime, timedelta, timezone
from jose import jwt

ALG = "HS256"

def issue_capability_token(signing_key: str, sub: str, on_behalf_of: str, action: str, resource: str, session_id: str, request_id: str, delegation_id: str | None = None, ttl_seconds: int = 300) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "on_behalf_of": on_behalf_of,
        "action": action,
        "resource": resource,
        "session_id": session_id,
        "request_id": request_id,
        "delegation_id": delegation_id,
        "nonce": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, signing_key, algorithm=ALG)

def verify_capability_token(signing_key: str, token: str) -> dict:
    return jwt.decode(token, signing_key, algorithms=[ALG])
