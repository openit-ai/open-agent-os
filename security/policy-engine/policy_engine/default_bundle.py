"""Default Policy Bundle — ships with product (Section 22)."""
from policy_model import PolicyBundle, PolicyRule, PolicySource, PolicyDecision

def default_bundle(tenant_id: str = "default") -> PolicyBundle:
    return PolicyBundle(
        id="default-bundle-v1",
        tenant_id=tenant_id,
        name="Default Policy Bundle",
        version="1.0.0",
        rules=[
            PolicyRule(id="deny-external-export", source=PolicySource.EXPLICIT_DENY, action="EXPORT", resource_pattern="*external*", effect=PolicyDecision.DENY, description="Block external export by default"),
            PolicyRule(id="allow-personal-read", source=PolicySource.PERSONAL_DELEGATION, action="READ", resource_pattern="gmail/user/*", effect=PolicyDecision.ALLOW),
            PolicyRule(id="allow-personal-calendar", source=PolicySource.PERSONAL_DELEGATION, action="READ", resource_pattern="calendar/user/*", effect=PolicyDecision.ALLOW),
            PolicyRule(id="allow-outline-read", source=PolicySource.DEFAULT_BUNDLE, action="READ", resource_pattern="outline/*", effect=PolicyDecision.ALLOW),
            PolicyRule(id="approval-merge", source=PolicySource.JIT_APPROVAL, action="MERGE", resource_pattern="github/*", effect=PolicyDecision.APPROVAL_REQUIRED),
            PolicyRule(id="approval-deploy", source=PolicySource.JIT_APPROVAL, action="DEPLOY", resource_pattern="*", effect=PolicyDecision.APPROVAL_REQUIRED),
            PolicyRule(id="approval-send", source=PolicySource.JIT_APPROVAL, action="SEND", resource_pattern="*", effect=PolicyDecision.APPROVAL_REQUIRED),
        ],
    )
