"""JIT Approval Workflow — Section 12, 23-24."""
from enum import Enum
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import hashlib, hmac, uuid

class ApprovalDecision(str, Enum):
    PENDING = "PENDING"
    APPROVED_ONCE = "APPROVED_ONCE"
    APPROVED_USER_ALWAYS = "APPROVED_USER_ALWAYS"
    APPROVED_GROUP_ALWAYS = "APPROVED_GROUP_ALWAYS"
    DENIED = "DENIED"

class ApprovalRequest(BaseModel):
    approval_id: str
    user_id: str
    agent_id: str
    resource: str
    action: str
    risk: str
    request_hash: str
    nonce: str
    expires_at: datetime
    signature: str | None = None

def create_approval_request(signing_key: str, user_id: str, agent_id: str, action: str, resource: str, risk: str = "HIGH", ttl_minutes: int = 60) -> ApprovalRequest:
    nonce = uuid.uuid4().hex
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    raw = f"{user_id}|{agent_id}|{action}|{resource}|{nonce}|{expires_at.isoformat()}"
    request_hash = hashlib.sha256(raw.encode()).hexdigest()
    sig = hmac.new(signing_key.encode(), request_hash.encode(), hashlib.sha256).hexdigest()
    return ApprovalRequest(
        approval_id=f"apr_{uuid.uuid4().hex[:12]}",
        user_id=user_id, agent_id=agent_id, resource=resource, action=action, risk=risk,
        request_hash=request_hash, nonce=nonce, expires_at=expires_at, signature=sig,
    )
