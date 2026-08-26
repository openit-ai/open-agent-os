"""Agent Context — Section 18. Every request carries this."""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional

class AgentContext(BaseModel):
    tenant_id: str = Field(description="Customer tenant")
    user_id: str   = Field(description="Human principal, e.g. employee:kim")
    agent_id: str  = Field(description="Agent principal, e.g. agent:assistant:kim")
    session_id: str
    trace_id: str
    request_id: str
    security_domain: str = "general"
    # populated when using personal credential
    credential_binding_id: Optional[str] = None
    delegation_id: Optional[str] = None

    def is_personal_credential_call(self) -> bool:
        return self.credential_binding_id is not None

    model_config = {"frozen": True}

class InternalAgentInterface:
    """Internal Agent Interface (Section 17) — canonical contract, ACP is adapter."""
    async def create_session(self, ctx: AgentContext) -> str: ...
    async def resume_session(self, session_id: str) -> AgentContext: ...
    async def send_prompt(self, ctx: AgentContext, prompt: str) -> None: ...
    async def stream_event(self, ctx: AgentContext): ...  # async generator
    async def request_permission(self, ctx: AgentContext, capability: dict) -> str: ...
    async def cancel_session(self, session_id: str) -> None: ...
    async def get_session_state(self, session_id: str) -> dict: ...
