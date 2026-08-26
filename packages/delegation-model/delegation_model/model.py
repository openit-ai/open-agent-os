"""Delegation & CredentialBinding — Sections 9, 10, 34."""
from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, Field
from datetime import datetime

class DelegationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"

class CredentialBindingStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"

class Delegation(BaseModel):
    """Section 34 Delegation"""
    id: str
    user_id: str
    agent_id: str
    provider: str  # google, microsoft, github ...
    scope: str     # gmail.read, calendar.readwrite ...
    status: DelegationStatus = DelegationStatus.ACTIVE
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

class CredentialBinding(BaseModel):
    """Section 34 CredentialBinding — secret_ref never holds plaintext token"""
    id: str
    delegation_id: str
    provider: str
    secret_ref: str  # opaque ref into Personal Credential Vault
    scope: str
    expires_at: datetime | None = None
    status: CredentialBindingStatus = CredentialBindingStatus.ACTIVE
    last_used_at: datetime | None = None

class CapabilityToken(BaseModel):
    """Section 26 — short-lived signed JWT-like token"""
    sub: str  # agent:assistant:kim
    on_behalf_of: str  # employee:kim
    action: str
    resource: str
    session_id: str
    request_id: str
    delegation_id: str | None = None
    expires_at: datetime
    nonce: str
    signature: str | None = None
