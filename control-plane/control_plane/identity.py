"""Identity Mapping — Mattermost/Slack user → Logical Personal Agent (Sections 14-15)"""
from dataclasses import dataclass

@dataclass(frozen=True)
class IdentityMapping:
    human_principal: str  # employee:kim
    agent_principal: str  # agent:assistant:kim
    tenant_id: str
    security_domain: str

def map_user_to_agent(user_id: str, tenant_id: str, security_domain: str = "general") -> IdentityMapping:
    # Logical Personal Agent is deterministic: 1 human = 1 agent principal
    agent_id = user_id.replace("employee:", "agent:assistant:")
    if not agent_id.startswith("agent:"):
        agent_id = f"agent:assistant:{user_id}"
    return IdentityMapping(human_principal=user_id, agent_principal=agent_id, tenant_id=tenant_id, security_domain=security_domain)
