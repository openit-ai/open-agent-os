"""Policy Model — Section 25. Order matters."""
from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, Field

class PolicyDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"

class PolicySource(str, Enum):
    EXPLICIT_DENY = "explicit_deny"
    SECURITY_BOUNDARY_DENY = "security_boundary_deny"
    PERSONAL_DELEGATION = "personal_delegation"
    PERSISTENT_USER_GRANT = "persistent_user_grant"
    GROUP_GRANT = "group_grant"
    DEFAULT_BUNDLE = "default_bundle"
    JIT_APPROVAL = "jit_approval"
    DEFAULT_DENY = "default_deny"

# Evaluation order (Section 25) — index = priority (lower wins)
POLICY_EVALUATION_ORDER: list[PolicySource] = [
    PolicySource.EXPLICIT_DENY,
    PolicySource.SECURITY_BOUNDARY_DENY,
    PolicySource.PERSONAL_DELEGATION,
    PolicySource.PERSISTENT_USER_GRANT,
    PolicySource.GROUP_GRANT,
    PolicySource.DEFAULT_BUNDLE,
    PolicySource.JIT_APPROVAL,
    PolicySource.DEFAULT_DENY,
]

class PolicyRule(BaseModel):
    id: str
    source: PolicySource
    action: str
    resource_pattern: str  # glob, e.g. gmail/user/kim/*
    effect: PolicyDecision
    priority: int = 0
    description: str | None = None

class PolicyBundle(BaseModel):
    id: str
    tenant_id: str
    name: str
    version: str
    rules: list[PolicyRule] = Field(default_factory=list)

class PolicyEvaluationRequest(BaseModel):
    tenant_id: str
    user_id: str
    agent_id: str
    action: str
    resource: str
    context: dict = Field(default_factory=dict)  # delegation_id, credential_binding_id, etc.

class PolicyEvaluationResult(BaseModel):
    decision: PolicyDecision
    matched_rule: PolicyRule | None = None
    source: PolicySource
    reason: str
    policy_version: str | None = None
