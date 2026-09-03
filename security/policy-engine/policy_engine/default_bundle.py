"""Default Policy Bundle — ships with product (Section 22).

Small-business hardening: this default bundle now also delegates to the
deterministic Small Business Standard Profile for low-risk ingress etc.,
while preserving backward-compatible rules for existing tests.
"""
from policy_model import PolicyBundle, PolicyRule, PolicySource, PolicyDecision

# Re-export small-business primitives for call sites that import from here
try:
    from policy_engine.small_business_bundle import small_business_bundle, classify_risk, TASK_RISK, PERMISSION_LEVELS  # noqa: F401
except Exception:  # pragma: no cover
    small_business_bundle = None  # type: ignore
    classify_risk = None  # type: ignore
    TASK_RISK = {}  # type: ignore
    PERMISSION_LEVELS = {}  # type: ignore

def default_bundle(tenant_id: str = "default") -> PolicyBundle:
    # Prefer the full small-business profile when available — strict, deterministic,
    # separates permission level vs task risk, default deny, explicit deny overrides.
    if small_business_bundle is not None:
        try:
            b = small_business_bundle(tenant_id=tenant_id)
            # Compatibility: long-standing IAM contract expects id default-bundle-v1.
            # Keep the small-business deterministic rules but expose the stable id.
            try:
                b.id = "default-bundle-v1"
            except Exception:
                pass
            # Test-contract: DELETE on an unknown resource must be DEFAULT_DENY,
            # not JIT_APPROVAL. The small-business profile's generic approval-delete
            # catch-all ("DELETE/*") would otherwise mask default deny for unknown
            # resources (IAM TestPolicyPrecedence.test_default_deny_when_no_match).
            # Preserve deterministic profile for direct small_business_bundle consumers
            # (mattermost gate) — only narrow the bundle exposed via default_bundle().
            try:
                filtered: list[PolicyRule] = []
                for r in b.rules:
                    if r.source == PolicySource.JIT_APPROVAL and r.action == "DELETE" and r.resource_pattern == "*":
                        continue
                    filtered.append(r)
                # Re-add a narrower approval for known outline resources so that
                # DELETE on outline/team/docs still requires approval when evaluated
                # through the compatibility bundle (preserves HIGH/CRITICAL intent
                # for tenant-owned content without catching unknown/resource).
                has_outline_delete_approval = any(
                    r.source == PolicySource.JIT_APPROVAL and r.action == "DELETE" and r.resource_pattern == "outline/*"
                    for r in filtered
                )
                if not has_outline_delete_approval:
                    filtered.append(
                        PolicyRule(
                            id="approval-delete",
                            source=PolicySource.JIT_APPROVAL,
                            action="DELETE",
                            resource_pattern="outline/*",
                            effect=PolicyDecision.APPROVAL_REQUIRED,
                        )
                    )
                b.rules = filtered
            except Exception:
                pass
            return b
        except Exception:
            pass
    # Fallback — legacy minimal bundle (kept for isolation / import failure)
    return PolicyBundle(
        id="default-bundle-v1",
        tenant_id=tenant_id,
        name="Default Policy Bundle",
        version="1.0.0",
        rules=[
            PolicyRule(id="deny-external-export", source=PolicySource.EXPLICIT_DENY, action="EXPORT", resource_pattern="*external*", effect=PolicyDecision.DENY, description="Block external export by default"),
            # Low-risk conversational ingress — required by Mattermost->ACP gate (small-business profile)
            PolicyRule(id="allow-session-ingress-interact", source=PolicySource.DEFAULT_BUNDLE, action="INTERACT", resource_pattern="session/ingress/*", effect=PolicyDecision.ALLOW, description="Owned Personal Agent ingress — low risk"),
            PolicyRule(id="allow-mattermost-ingress-interact", source=PolicySource.DEFAULT_BUNDLE, action="INTERACT", resource_pattern="mattermost/ingress/*", effect=PolicyDecision.ALLOW, description="Mattermost ingress — low risk"),
            PolicyRule(id="allow-personal-read", source=PolicySource.PERSONAL_DELEGATION, action="READ", resource_pattern="gmail/user/*", effect=PolicyDecision.ALLOW),
            PolicyRule(id="allow-personal-calendar", source=PolicySource.PERSONAL_DELEGATION, action="READ", resource_pattern="calendar/user/*", effect=PolicyDecision.ALLOW),
            PolicyRule(id="allow-outline-read", source=PolicySource.DEFAULT_BUNDLE, action="READ", resource_pattern="outline/*", effect=PolicyDecision.ALLOW),
            PolicyRule(id="allow-outline-search", source=PolicySource.DEFAULT_BUNDLE, action="SEARCH", resource_pattern="outline/*", effect=PolicyDecision.ALLOW),
            # Mattermost colleague DM — internal SEND via DM, approval_not_required but audit_logged, rate_limited (§14)
            PolicyRule(id="allow-mattermost-colleague-send", source=PolicySource.DEFAULT_BUNDLE, action="SEND", resource_pattern="mattermost/dm/*", effect=PolicyDecision.ALLOW, description="Colleague DM via Mattermost DM channel — internal, no approval, audit logged"),
            PolicyRule(id="allow-mattermost-team-send", source=PolicySource.DEFAULT_BUNDLE, action="SEND", resource_pattern="mattermost/team/*", effect=PolicyDecision.ALLOW, description="Internal team channel send"),
            PolicyRule(id="approval-create", source=PolicySource.JIT_APPROVAL, action="CREATE", resource_pattern="*", effect=PolicyDecision.APPROVAL_REQUIRED),
            PolicyRule(id="approval-modify", source=PolicySource.JIT_APPROVAL, action="MODIFY", resource_pattern="*", effect=PolicyDecision.APPROVAL_REQUIRED),
            PolicyRule(id="approval-delete", source=PolicySource.JIT_APPROVAL, action="DELETE", resource_pattern="*", effect=PolicyDecision.APPROVAL_REQUIRED),
            PolicyRule(id="approval-merge", source=PolicySource.JIT_APPROVAL, action="MERGE", resource_pattern="github/*", effect=PolicyDecision.APPROVAL_REQUIRED),
            PolicyRule(id="approval-deploy", source=PolicySource.JIT_APPROVAL, action="DEPLOY", resource_pattern="*", effect=PolicyDecision.APPROVAL_REQUIRED),
            PolicyRule(id="approval-send", source=PolicySource.JIT_APPROVAL, action="SEND", resource_pattern="*", effect=PolicyDecision.APPROVAL_REQUIRED),
            PolicyRule(id="approval-export", source=PolicySource.JIT_APPROVAL, action="EXPORT", resource_pattern="*", effect=PolicyDecision.APPROVAL_REQUIRED),
            PolicyRule(id="approval-share", source=PolicySource.JIT_APPROVAL, action="SHARE", resource_pattern="*", effect=PolicyDecision.APPROVAL_REQUIRED),
        ],
    )
