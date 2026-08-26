"""Session Router — create/resume/route sessions (Section 7.1)"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class SessionRecord:
    session_id: str
    tenant_id: str
    user_id: str
    agent_id: str
    trace_id: str
    security_domain: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    hermes_worker: str | None = None

def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:16]}"
def new_trace_id() -> str:
    return f"trace_{uuid.uuid4().hex[:16]}"
def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"
