"""Audit Model — Sections 30-31. Hash-chain + signed checkpoint."""
from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, Field
from datetime import datetime
import hashlib, json

class AuditEventType(str, Enum):
    USER_MESSAGE = "USER_MESSAGE"
    AGENT_SESSION_START = "AGENT_SESSION_START"
    MODEL_REQUEST = "MODEL_REQUEST"
    MODEL_RESPONSE = "MODEL_RESPONSE"
    PERSONAL_CREDENTIAL_USE = "PERSONAL_CREDENTIAL_USE"
    DELEGATION_CREATED = "DELEGATION_CREATED"
    DELEGATION_REVOKED = "DELEGATION_REVOKED"
    SKILL_REQUEST = "SKILL_REQUEST"
    POLICY_DECISION = "POLICY_DECISION"
    APPROVAL_REQUEST = "APPROVAL_REQUEST"
    APPROVAL_DECISION = "APPROVAL_DECISION"
    CAPABILITY_ISSUED = "CAPABILITY_ISSUED"
    MCP_TOOL_CALL = "MCP_TOOL_CALL"
    DATA_ACCESS = "DATA_ACCESS"
    TOOL_RESULT = "TOOL_RESULT"
    MEMORY_WRITE = "MEMORY_WRITE"
    MEMORY_INVALIDATE = "MEMORY_INVALIDATE"
    EXTERNAL_EXPORT = "EXTERNAL_EXPORT"
    AGENT_RESPONSE = "AGENT_RESPONSE"
    SESSION_END = "SESSION_END"

class AuditEvent(BaseModel):
    event_id: str
    event_type: AuditEventType
    timestamp: datetime
    tenant_id: str
    user_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None
    trace_id: str | None = None
    request_id: str | None = None
    resource: str | None = None
    action: str | None = None
    decision: str | None = None
    policy_version: str | None = None
    delegation_id: str | None = None
    credential_binding_id: str | None = None
    tool_name: str | None = None
    parameters_hash: str | None = None
    result_hash: str | None = None
    previous_hash: str | None = None
    event_hash: str | None = None

    def canonical_payload(self) -> str:
        d = self.model_dump(exclude={"event_hash"}, mode="json")
        return json.dumps(d, sort_keys=True, ensure_ascii=False)

    def compute_hash(self) -> str:
        h = hashlib.sha256()
        if self.previous_hash:
            h.update(self.previous_hash.encode())
        h.update(self.canonical_payload().encode())
        return h.hexdigest()

class AuditCheckpoint(BaseModel):
    chain_head_hash: str
    event_count: int
    created_at: datetime
    signature: str  # detached signature over chain_head_hash
