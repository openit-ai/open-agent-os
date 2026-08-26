"""Common domain primitives — Phase 0 Architecture Contract."""
from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, Field
from typing import Literal
from datetime import datetime

TenantId = str
UserId = str       # employee:kim
AgentId = str      # agent:assistant:kim
SessionId = str
TraceId = str
RequestId = str
ResourceId = str

class SecurityDomain(str, Enum):
    GENERAL = "general"
    DEVELOPMENT = "development"
    FINANCE_HR = "finance_hr"
    ADMIN = "admin"
    HIGH_RISK_EPHEMERAL = "high_risk_ephemeral"

class Action(str, Enum):
    READ = "READ"
    SEARCH = "SEARCH"
    CREATE = "CREATE"
    MODIFY = "MODIFY"
    DELETE = "DELETE"
    EXECUTE = "EXECUTE"
    EXPORT = "EXPORT"
    SHARE = "SHARE"
    APPROVE = "APPROVE"
    ADMIN = "ADMIN"
    SEND = "SEND"
    MERGE = "MERGE"
    DEPLOY = "DEPLOY"
    PAY = "PAY"

class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

class DataClassification(str, Enum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    PII = "PII"
    SECRET = "SECRET"

class Resource(BaseModel):
    """Canonical resource identifier:  <domain>/<scope>/<path>  e.g. gmail/user/kim/*"""
    raw: str = Field(description="Canonical resource string")
    domain: str | None = None
    scope: str | None = None

    def __str__(self) -> str:
        return self.raw

class Capability(BaseModel):
    """Capability = Action + Resource + Scope  (Section 19)"""
    action: Action
    resource: str
    scope: str | None = None  # e.g. user/kim, group/dev
    classification: DataClassification | None = None

class Tenant(BaseModel):
    id: TenantId
    name: str
    created_at: datetime | None = None

class User(BaseModel):
    id: UserId
    tenant_id: TenantId
    email: str
    display_name: str
    groups: list[str] = Field(default_factory=list)
    security_domain: SecurityDomain = SecurityDomain.GENERAL

class Group(BaseModel):
    id: str
    tenant_id: TenantId
    name: str

class Agent(BaseModel):
    """Logical Personal Agent — 1:1 with User (Section 14)"""
    id: AgentId
    user_id: UserId
    tenant_id: TenantId
    security_domain: SecurityDomain
    created_at: datetime | None = None

class Session(BaseModel):
    id: SessionId
    tenant_id: TenantId
    user_id: UserId
    agent_id: AgentId
    trace_id: TraceId
    security_domain: SecurityDomain
    created_at: datetime | None = None
