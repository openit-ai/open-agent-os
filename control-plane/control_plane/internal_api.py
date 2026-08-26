"""Internal Agent Interface — Section 17 canonical contract (ACP is adapter)."""
from __future__ import annotations
from typing import AsyncGenerator, Protocol
from pydantic import BaseModel

class CreateSessionRequest(BaseModel):
    tenant_id: str
    user_id: str
    security_domain: str = "general"

class CreateSessionResponse(BaseModel):
    session_id: str
    agent_id: str
    trace_id: str

class SendPromptRequest(BaseModel):
    session_id: str
    prompt: str
    request_id: str | None = None

class StreamEvent(BaseModel):
    type: str  # token | tool_call | approval_request | done | error
    data: dict
    trace_id: str | None = None

class InternalAgentInterface(Protocol):
    async def create_session(self, req: CreateSessionRequest) -> CreateSessionResponse: ...
    async def resume_session(self, session_id: str, caller_user_id: str) -> dict: ...
    async def send_prompt(self, req: SendPromptRequest, caller_user_id: str) -> None: ...
    async def stream_events(self, session_id: str, caller_user_id: str) -> AsyncGenerator[StreamEvent, None]: ...
    async def request_permission(self, session_id: str, capability: dict, caller_user_id: str) -> str: ...
    async def cancel_session(self, session_id: str, caller_user_id: str) -> None: ...
    async def get_session_state(self, session_id: str, caller_user_id: str) -> dict: ...
