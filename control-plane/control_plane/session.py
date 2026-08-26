"""Session Router & Store — create/resume/route with cross-user isolation (Section 7.1, 14).

Requirements (Workstream A 완료조건):
- 1 user = 1 logical agent (via identity.map_user_to_agent)
- cross-user session isolation: session owner check on every access
- stream response 정상 (session holds stream buffer)
- user context 유지 (AgentContext in session)
"""
from __future__ import annotations
import uuid
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:16]}"
def new_trace_id() -> str:
    return f"trace_{uuid.uuid4().hex[:16]}"
def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"

@dataclass
class SessionRecord:
    session_id: str
    tenant_id: str
    user_id: str
    agent_id: str
    trace_id: str
    security_domain: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    hermes_worker: str | None = None
    # stream buffer (in-memory; Redis for prod)
    prompt_history: list[dict] = field(default_factory=list)
    stream_events: list[dict] = field(default_factory=list)
    status: str = "active"  # active | cancelled | ended

    def assert_owner(self, caller_user_id: str) -> None:
        if caller_user_id != self.user_id:
            raise PermissionError(f"cross-user session access denied: session owned by {self.user_id}, caller {caller_user_id}")

    def to_agent_context(self, request_id: str | None = None) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "request_id": request_id or new_request_id(),
            "security_domain": self.security_domain,
        }

class SessionStore:
    """In-memory store — replace with Redis/Postgres for prod (Section 7.1 하지 않는 것: 기업 데이터 저장 아님, 세션 메타만)."""
    def __init__(self):
        self._store: dict[str, SessionRecord] = {}

    def create(self, tenant_id: str, user_id: str, agent_id: str, security_domain: str = "general", hermes_worker: str | None = None) -> SessionRecord:
        rec = SessionRecord(
            session_id=new_session_id(),
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            trace_id=new_trace_id(),
            security_domain=security_domain,
            hermes_worker=hermes_worker,
        )
        self._store[rec.session_id] = rec
        return rec

    def get(self, session_id: str, caller_user_id: str) -> SessionRecord:
        rec = self._store.get(session_id)
        if not rec:
            raise KeyError(f"session not found: {session_id}")
        rec.assert_owner(caller_user_id)
        return rec

    def get_any(self, session_id: str) -> SessionRecord | None:
        return self._store.get(session_id)

    def append_prompt(self, session_id: str, caller_user_id: str, prompt: str, request_id: str) -> None:
        rec = self.get(session_id, caller_user_id)
        rec.prompt_history.append({"prompt": prompt, "request_id": request_id, "at": datetime.now(timezone.utc).isoformat()})
        rec.updated_at = datetime.now(timezone.utc)

    def append_stream_event(self, session_id: str, event: dict) -> None:
        if rec := self._store.get(session_id):
            rec.stream_events.append(event)

    def cancel(self, session_id: str, caller_user_id: str) -> None:
        rec = self.get(session_id, caller_user_id)
        rec.status = "cancelled"

# Global singleton for dev — prod uses dependency injection
session_store = SessionStore()
