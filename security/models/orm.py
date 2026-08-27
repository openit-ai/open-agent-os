"""SQLAlchemy ORM tables — keeps existing dataclass/Pydantic fields verbatim.

Tables:
  delegations, credential_bindings, approval_requests, audit_events,
  session_records, vault_credentials (encrypted column)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, DateTime, Index, ForeignKey, LargeBinary
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import JSON as GenericJSON
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

# Use JSONB on Postgres, fallback to generic JSON for SQLite tests
try:
    from sqlalchemy.dialects.postgresql import JSONB as _JSONB  # noqa
    JSONType = JSONB
except Exception:  # pragma: no cover
    from sqlalchemy import JSON as JSONType  # type: ignore


class DelegationORM(Base):
    __tablename__ = "delegations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(256), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_delegations_user_provider", "user_id", "provider"),)


class CredentialBindingORM(Base):
    __tablename__ = "credential_bindings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    delegation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("delegations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_ref: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    scope: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovalRequestORM(Base):
    __tablename__ = "approval_requests"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(256), nullable=False)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    risk: Mapped[str] = mapped_column(String(16), nullable=False, default="HIGH")
    request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    group_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEventORM(Base):
    __tablename__ = "audit_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delegation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credential_binding_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parameters_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    previous_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (Index("ix_audit_tenant_time", "tenant_id", "timestamp"),)


class SessionRecordORM(Base):
    __tablename__ = "session_records"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(256), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    security_domain: Mapped[str] = mapped_column(String(32), nullable=False, default="general")
    hermes_worker: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # JSON columns — use Generic JSON for SQLite compat
    prompt_history: Mapped[list] = mapped_column(GenericJSON, nullable=False, default=list)
    stream_events: Mapped[list] = mapped_column(GenericJSON, nullable=False, default=list)

    __table_args__ = (Index("ix_session_user", "user_id", "tenant_id"),)


class VaultCredentialORM(Base):
    """DB-encrypted vault row — encrypted_token is Fernet ciphertext (bytes as LargeBinary)."""

    __tablename__ = "vault_credentials"

    secret_ref: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    owner_agent_id: Mapped[str] = mapped_column(String(256), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(256), nullable=False)
    encrypted_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
