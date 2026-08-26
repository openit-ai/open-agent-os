"""Personal Agent Profile — Section 14-15. Logical Personal Agent = user's digital work identity."""
from __future__ import annotations
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from typing import Literal

class PersonalAgentProfile(BaseModel):
    """1 user = 1 logical agent. Persisted in Control Plane DB."""
    agent_id: str = Field(description="agent:assistant:{user_id}")
    user_id: str = Field(description="employee:kim")
    tenant_id: str
    display_name: str | None = None
    security_domain: str = "general"
    # Preferences (user-owned)
    locale: str = "ko-KR"
    timezone: str = "Asia/Seoul"
    # Delegated credential refs (opaque — never plaintext)
    delegation_ids: list[str] = Field(default_factory=list)
    # Enterprise grants (JIT or persistent)
    granted_capabilities: list[str] = Field(default_factory=list)
    # Policy bindings
    policy_bundle_version: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_personal_agent(self) -> bool:
        return self.agent_id.startswith("agent:assistant:")

def derive_agent_id(user_id: str) -> str:
    """Deterministic 1:1 mapping — Section 14."""
    if user_id.startswith("employee:"):
        return user_id.replace("employee:", "agent:assistant:", 1)
    if user_id.startswith("agent:"):
        return user_id  # already agent
    return f"agent:assistant:{user_id}"

def make_profile(user_id: str, tenant_id: str, display_name: str | None = None, security_domain: str = "general") -> PersonalAgentProfile:
    return PersonalAgentProfile(
        agent_id=derive_agent_id(user_id),
        user_id=user_id,
        tenant_id=tenant_id,
        display_name=display_name,
        security_domain=security_domain,
    )
