"""Identity Mapping — Mattermost/Slack user → Logical Personal Agent (Sections 14-15)

Invariants:
- 1 human principal = 1 agent principal (deterministic)
- Agent Permission <= User Permission (enforced downstream by Policy Engine)
- Cross-user mapping is forbidden — caller must prove user ownership
"""
from dataclasses import dataclass
from .personal_agent import derive_agent_id, make_profile, PersonalAgentProfile

@dataclass(frozen=True)
class IdentityMapping:
    human_principal: str  # employee:kim
    agent_principal: str  # agent:assistant:kim
    tenant_id: str
    security_domain: str
    profile: PersonalAgentProfile

    def assert_owner(self, caller_user_id: str) -> None:
        if caller_user_id != self.human_principal:
            raise PermissionError(f"cross-user identity mapping denied: caller={caller_user_id} owner={self.human_principal}")

def map_user_to_agent(user_id: str, tenant_id: str, security_domain: str = "general", display_name: str | None = None) -> IdentityMapping:
    if not user_id or not tenant_id:
        raise ValueError("user_id and tenant_id are required")
    if ":" not in user_id:
        raise ValueError(f"user_id must be namespaced (e.g. employee:kim), got {user_id!r}")
    profile = make_profile(user_id, tenant_id, display_name, security_domain)
    return IdentityMapping(
        human_principal=user_id,
        agent_principal=profile.agent_id,
        tenant_id=tenant_id,
        security_domain=security_domain,
        profile=profile,
    )
