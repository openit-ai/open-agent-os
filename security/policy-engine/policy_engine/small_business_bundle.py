"""Small-Business Standard Policy Profile — deterministic gate.

Separation of concerns:
  * User Permission Level (who you are) — maps to PolicySource / scope
    - guest(0) < member(1) < manager(2) < owner(3)
    Determined by authenticated identity + ownership proof (IdentityMapping),
    NOT by prompt content. Enforced via ownership checks before policy eval.

  * Task Risk (what you want to do) — maps to Action+Resource risk tier
    - LOW      : read-only company knowledge, owned Personal Agent ingress
    - MEDIUM   : internal messaging within tenant (Mattermost DM/team)
    - HIGH     : state-changing writes (CREATE/MODIFY) within tenant
    - CRITICAL : external / destructive / privileged (SEND external, MERGE/DEPLOY/DELETE/EXPORT/SHARE/PAY/ADMIN)
    Determined deterministically from canonical Action+Resource, never by LLM.

Defaults:
  * Default DENY (PolicyEngine DEFAULT_DENY) — no rule => DENY.
  * ALLOW only for authenticated/owned Mattermost Personal Agent conversational ingress (INTERACT)
    and authorized read-only Outline/company knowledge + owned personal read.
  * APPROVAL_REQUIRED for HIGH/CRITICAL and any writes/external.
  * Explicit DENY overrides any ALLOW (strict order).

This module is the canonical small-business profile. Import as:
  from policy_engine.small_business_bundle import small_business_bundle, classify_risk, TASK_RISK
"""
from __future__ import annotations

from policy_model import PolicyBundle, PolicyDecision, PolicyRule, PolicySource

# ── Permission vs Risk — deterministic, never LLM ─────────────────────

# User permission tiers (informational; enforcement is via source/order + ownership)
PERMISSION_LEVELS: dict[str, int] = {"guest": 0, "member": 1, "manager": 2, "owner": 3}

# Task risk tiers (deterministic classification, see classify_risk)
TASK_RISK: dict[str, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

# Actions that are by definition CRITICAL regardless of resource
_CRITICAL_ACTIONS = frozenset({"MERGE", "DEPLOY", "DELETE", "EXPORT", "PAY", "ADMIN", "SHARE"})

# Actions that are HIGH when applied to any resource (state-changing)
_HIGH_ACTIONS = frozenset({"CREATE", "MODIFY"})

# SEND is MEDIUM if internal (mattermost/dm|team), CRITICAL if external/wildcard
# INTERACT/READ/SEARCH are LOW for allowed resources, otherwise denied by default


def classify_risk(action: str, resource: str) -> str:
    """Deterministically classify (action, resource) into LOW/MEDIUM/HIGH/CRITICAL.

    Never uses LLM. Pure fnmatch-style heuristics.
    """
    a = (action or "").strip().upper()
    r = (resource or "").strip().lower()

    # Explicit external/destructive signals
    if "external" in r:
        return "CRITICAL"
    if a in _CRITICAL_ACTIONS:
        return "CRITICAL"
    # production / sensitive prefix
    if r.startswith("production/") or "private" in r:
        # private outline read is sensitive — treat as CRITICAL for explicit deny, but
        # write to production is CRITICAL anyway
        if a in ("DELETE", "MODIFY", "CREATE", "EXPORT"):
            return "CRITICAL"
    if a in _HIGH_ACTIONS:
        return "HIGH"
    if a == "SEND":
        # internal mattermost DM/team is MEDIUM, everything else CRITICAL
        if r.startswith("mattermost/dm/") or r.startswith("mattermost/team/"):
            return "MEDIUM"
        return "CRITICAL"
    if a == "INTERACT":
        return "LOW"
    if a in ("READ", "SEARCH"):
        # outline/* read is LOW, personal read LOW, everything else will be DENY anyway
        if r.startswith("outline/") or "/user/" in r:
            return "LOW"
        return "LOW"
    # fallback — treat as HIGH so it requires approval rather than silent allow
    return "HIGH"


def requires_approval(action: str, resource: str) -> bool:
    return classify_risk(action, resource) in ("HIGH", "CRITICAL", "MEDIUM")


def small_business_bundle(tenant_id: str = "default") -> PolicyBundle:
    """Deterministic small-business default bundle.

    Rules are ordered by PolicySource priority (engine respects POLICY_EVALUATION_ORDER).
    Within a source, priority+id determines tie-break.
    """
    rules: list[PolicyRule] = [
        # ── EXPLICIT_DENY — overrides everything ──────────────────────
        PolicyRule(
            id="deny-external-export",
            source=PolicySource.EXPLICIT_DENY,
            action="EXPORT",
            resource_pattern="*external*",
            effect=PolicyDecision.DENY,
            description="Block external export by default (small-business)",
        ),
        PolicyRule(
            id="deny-external-share",
            source=PolicySource.EXPLICIT_DENY,
            action="SHARE",
            resource_pattern="*external*",
            effect=PolicyDecision.DENY,
            description="Block external share",
        ),
        PolicyRule(
            id="deny-external-send",
            source=PolicySource.EXPLICIT_DENY,
            action="SEND",
            resource_pattern="*external*",
            effect=PolicyDecision.DENY,
            description="Block external send — explicit deny overrides",
        ),
        PolicyRule(
            id="deny-sensitive-outline-private-read",
            source=PolicySource.EXPLICIT_DENY,
            action="READ",
            resource_pattern="outline/private/*",
            effect=PolicyDecision.DENY,
            description="Explicit deny sensitive outline private (tests precedence)",
        ),
        PolicyRule(
            id="deny-sensitive-outline-private-search",
            source=PolicySource.EXPLICIT_DENY,
            action="SEARCH",
            resource_pattern="outline/private/*",
            effect=PolicyDecision.DENY,
            description="Explicit deny sensitive outline private search",
        ),
        PolicyRule(
            id="deny-production-delete",
            source=PolicySource.EXPLICIT_DENY,
            action="DELETE",
            resource_pattern="production/*",
            effect=PolicyDecision.DENY,
            description="Never delete production without explicit grant",
        ),
        PolicyRule(
            id="deny-admin-by-default",
            source=PolicySource.EXPLICIT_DENY,
            action="ADMIN",
            resource_pattern="*",
            effect=PolicyDecision.DENY,
            description="Admin requires explicit grant/approval",
        ),
        # ── SECURITY_BOUNDARY_DENY (tenant/isolation) ───────────────
        # Keep minimal — real cross-tenant check is in connector; here we deny
        # any production deploy without boundary approval already covered.
        # ── PERSONAL_DELEGATION — owned personal read only ──────────
        PolicyRule(
            id="allow-personal-gmail-read",
            source=PolicySource.PERSONAL_DELEGATION,
            action="READ",
            resource_pattern="gmail/user/*",
            effect=PolicyDecision.ALLOW,
        ),
        PolicyRule(
            id="allow-personal-gmail-search",
            source=PolicySource.PERSONAL_DELEGATION,
            action="SEARCH",
            resource_pattern="gmail/user/*",
            effect=PolicyDecision.ALLOW,
        ),
        PolicyRule(
            id="allow-personal-calendar-read",
            source=PolicySource.PERSONAL_DELEGATION,
            action="READ",
            resource_pattern="calendar/user/*",
            effect=PolicyDecision.ALLOW,
        ),
        PolicyRule(
            id="allow-personal-calendar-search",
            source=PolicySource.PERSONAL_DELEGATION,
            action="SEARCH",
            resource_pattern="calendar/user/*",
            effect=PolicyDecision.ALLOW,
        ),
        PolicyRule(
            id="allow-personal-drive-read",
            source=PolicySource.PERSONAL_DELEGATION,
            action="READ",
            resource_pattern="drive/user/*",
            effect=PolicyDecision.ALLOW,
        ),
        PolicyRule(
            id="allow-personal-drive-search",
            source=PolicySource.PERSONAL_DELEGATION,
            action="SEARCH",
            resource_pattern="drive/user/*",
            effect=PolicyDecision.ALLOW,
        ),
        PolicyRule(
            id="allow-personal-tasks-read",
            source=PolicySource.PERSONAL_DELEGATION,
            action="READ",
            resource_pattern="tasks/user/*",
            effect=PolicyDecision.ALLOW,
        ),
        PolicyRule(
            id="allow-personal-tasks-search",
            source=PolicySource.PERSONAL_DELEGATION,
            action="SEARCH",
            resource_pattern="tasks/user/*",
            effect=PolicyDecision.ALLOW,
        ),
        # ── DEFAULT_BUNDLE — small-business standard LOW/MEDIUM allows ──
        # Conversational ingress: authenticated/owned Personal Agent session
        # Resource convention: session/ingress/{tenant}/{session_id} or mattermost/ingress/*
        PolicyRule(
            id="allow-session-ingress-interact",
            source=PolicySource.DEFAULT_BUNDLE,
            action="INTERACT",
            resource_pattern="session/ingress/*",
            effect=PolicyDecision.ALLOW,
            description="Owned Personal Agent conversational ingress — low risk",
        ),
        PolicyRule(
            id="allow-mattermost-ingress-interact",
            source=PolicySource.DEFAULT_BUNDLE,
            action="INTERACT",
            resource_pattern="mattermost/ingress/*",
            effect=PolicyDecision.ALLOW,
            description="Mattermost ingress — low risk chat",
        ),
        # Fallback: EXECUTE is sometimes used for ingress compat
        PolicyRule(
            id="allow-session-ingress-execute",
            source=PolicySource.DEFAULT_BUNDLE,
            action="EXECUTE",
            resource_pattern="session/ingress/*",
            effect=PolicyDecision.ALLOW,
            description="Compat: EXECUTE on session ingress treated as low-risk",
        ),
        # Read-only company knowledge (Outline) — must be after explicit deny
        PolicyRule(
            id="allow-outline-read",
            source=PolicySource.DEFAULT_BUNDLE,
            action="READ",
            resource_pattern="outline/*",
            effect=PolicyDecision.ALLOW,
            description="Read-only company knowledge",
        ),
        PolicyRule(
            id="allow-outline-search",
            source=PolicySource.DEFAULT_BUNDLE,
            action="SEARCH",
            resource_pattern="outline/*",
            effect=PolicyDecision.ALLOW,
            description="Search company knowledge",
        ),
        # Internal teammate messaging — MEDIUM, still allowed without approval
        PolicyRule(
            id="allow-mattermost-colleague-dm",
            source=PolicySource.DEFAULT_BUNDLE,
            action="SEND",
            resource_pattern="mattermost/dm/*",
            effect=PolicyDecision.ALLOW,
            description="Internal DM — medium risk, tenant-local",
        ),
        PolicyRule(
            id="allow-mattermost-team-send",
            source=PolicySource.DEFAULT_BUNDLE,
            action="SEND",
            resource_pattern="mattermost/team/*",
            effect=PolicyDecision.ALLOW,
            description="Internal team channel send",
        ),
        # ── JIT_APPROVAL — HIGH/CRITICAL require approval ─────────────
        PolicyRule(
            id="approval-create",
            source=PolicySource.JIT_APPROVAL,
            action="CREATE",
            resource_pattern="*",
            effect=PolicyDecision.APPROVAL_REQUIRED,
        ),
        PolicyRule(
            id="approval-modify",
            source=PolicySource.JIT_APPROVAL,
            action="MODIFY",
            resource_pattern="*",
            effect=PolicyDecision.APPROVAL_REQUIRED,
        ),
        # Approve deletes only for known resource domains. Unknown resources
        # must fall through to PolicyEngine's DEFAULT_DENY.
        *[
            PolicyRule(
                id=f"approval-delete-{domain.replace('/', '-')}",
                source=PolicySource.JIT_APPROVAL,
                action="DELETE",
                resource_pattern=f"{domain}/*",
                effect=PolicyDecision.APPROVAL_REQUIRED,
            )
            for domain in (
                "outline",
                "gmail",
                "gmail/user",
                "calendar",
                "calendar/user",
                "drive",
                "drive/user",
                "tasks",
                "tasks/user",
                "github",
                "mattermost",
                "session",
                "crm",
                "erp",
                "slack",
                "notion",
                "iam",
                "production",
            )
        ],
        PolicyRule(
            id="approval-send",
            source=PolicySource.JIT_APPROVAL,
            action="SEND",
            resource_pattern="*",
            effect=PolicyDecision.APPROVAL_REQUIRED,
        ),
        PolicyRule(
            id="approval-merge",
            source=PolicySource.JIT_APPROVAL,
            action="MERGE",
            resource_pattern="*",
            effect=PolicyDecision.APPROVAL_REQUIRED,
        ),
        PolicyRule(
            id="approval-deploy",
            source=PolicySource.JIT_APPROVAL,
            action="DEPLOY",
            resource_pattern="*",
            effect=PolicyDecision.APPROVAL_REQUIRED,
        ),
        PolicyRule(
            id="approval-export",
            source=PolicySource.JIT_APPROVAL,
            action="EXPORT",
            resource_pattern="*",
            effect=PolicyDecision.APPROVAL_REQUIRED,
        ),
        PolicyRule(
            id="approval-share",
            source=PolicySource.JIT_APPROVAL,
            action="SHARE",
            resource_pattern="*",
            effect=PolicyDecision.APPROVAL_REQUIRED,
        ),
        PolicyRule(
            id="approval-pay",
            source=PolicySource.JIT_APPROVAL,
            action="PAY",
            resource_pattern="*",
            effect=PolicyDecision.APPROVAL_REQUIRED,
        ),
        PolicyRule(
            id="approval-execute",
            source=PolicySource.JIT_APPROVAL,
            action="EXECUTE",
            resource_pattern="*",
            effect=PolicyDecision.APPROVAL_REQUIRED,
        ),
        PolicyRule(
            id="approval-admin",
            source=PolicySource.JIT_APPROVAL,
            action="ADMIN",
            resource_pattern="*",
            effect=PolicyDecision.APPROVAL_REQUIRED,
        ),
        PolicyRule(
            id="approval-interact-high",
            source=PolicySource.JIT_APPROVAL,
            action="INTERACT",
            resource_pattern="*",
            effect=PolicyDecision.APPROVAL_REQUIRED,
            description="Catch-all interact outside allowed ingress",
        ),
    ]
    return PolicyBundle(
        id="small-business-bundle-v1",
        tenant_id=tenant_id,
        name="Small Business Standard Profile v1",
        version="1.0.0",
        rules=rules,
    )


# Back-compat alias — some call sites import small_business_bundle as default
default_small_business_bundle = small_business_bundle
