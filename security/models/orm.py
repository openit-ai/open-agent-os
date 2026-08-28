"""SQLAlchemy ORM tables — keeps existing dataclass/Pydantic fields verbatim.

Tables:
  delegations, credential_bindings, approval_requests, audit_events,
  session_records, vault_credentials (encrypted column),
  memories, memory_sources, admin_state  — v1.6 §27 persistent memory (pgvector ready)
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import String, Text, DateTime, Index, ForeignKey, LargeBinary, Integer, Float
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

# ── pgvector ready: Vector(1536) on Postgres, fallback to Text for SQLite/tests ──
try:
    from pgvector.sqlalchemy import Vector as _PgVector  # type: ignore

    _VECTOR_1536 = _PgVector(1536)  # type: ignore
except Exception:  # pragma: no cover - pgvector not installed or SQLite
    _VECTOR_1536 = Text  # fallback column type for sqlite compat


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


class ApprovalNonceORM(Base):
    """Replay-protection nonces — persists _seen_nonces across restarts.

    nonce TEXT PK, created_at, expires_at. TTL 300s (token expiry) — expired
    rows are GC'd on each check/insert. DB-backed primary; in-memory set is
    fallback when DATABASE_URL is not configured or DB unreachable.
    """

    __tablename__ = "approval_nonces"

    nonce: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (Index("ix_approval_nonces_expires_at", "expires_at"),)


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
    """DB-encrypted vault row — encrypted_token is Fernet ciphertext (bytes as LargeBinary).

    Phase B externalization: encrypted_token is nullable (NULL when secret_ref
    only is stored and bytes live in external backend). secret_ref remains PK.
    vault_backend / vault_path / version added for migration observability
    (all nullable for zero-downtime migration).
    """

    __tablename__ = "vault_credentials"

    secret_ref: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    owner_agent_id: Mapped[str] = mapped_column(String(256), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(256), nullable=False)
    # Nullable for externalization: NULL when external backend holds the secret
    encrypted_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Externalization metadata (nullable until fully migrated)
    vault_backend: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vault_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ── v1.6 §27 Persistent Memory (pgvector ready, sqlite compatible) ────────────
# Phase B: §27.5 missing columns + memory_embeddings + memory_access_bindings
# sqlite-compat: Text for enums, nullable columns for add_column, no pg-only server_default

class MemoryORM(Base):
    """Persistent memory — tenant-scoped, per-user/agent, pgvector embedding.

    Phase B extensions (§27.5): namespace, owner_type/owner_id, memory_type,
    summary, classification, source_resource_type, retention_policy, expires_at,
    invalidated_at/invalidation_reason, source_acl_version, source_delegation_id.
    All new columns nullable for sqlite compat + zero-downtime migration.
    """

    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, default="episodic")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # pgvector Vector(1536) when available, else Text fallback for sqlite
    embedding: Mapped[str | None] = mapped_column(_VECTOR_1536, nullable=True)  # type: ignore[arg-type]
    source_ids: Mapped[list | None] = mapped_column(GenericJSON, nullable=True, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # ── Phase B: §27.5 missing columns (all nullable, Text for enums) ──
    namespace: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    memory_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_resource_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_acl_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_delegation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    retention_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_memories_tenant_user", "tenant_id", "user_id"),
        Index("ix_memories_tenant_agent", "tenant_id", "agent_id"),
        Index("ix_memories_namespace", "namespace"),
        Index("ix_memories_owner", "owner_type", "owner_id"),
        Index("ix_memories_classification", "classification"),
        Index("ix_memories_expires_at", "expires_at"),
        Index("ix_memories_invalidated_at", "invalidated_at"),
    )


class MemorySourceORM(Base):
    """Provenance source for a memory chunk — links memory to origin resource."""

    __tablename__ = "memory_sources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    memory_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, default="document")
    source_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", GenericJSON, nullable=True, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_memory_sources_memory", "memory_id"),)


class MemoryEmbeddingORM(Base):
    """Separate vector table — one row per memory, pgvector ready (Phase B)."""

    __tablename__ = "memory_embeddings"

    id: Mapped[str] = mapped_column(
        String(64), ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True
    )
    embedding: Mapped[str | None] = mapped_column(_VECTOR_1536, nullable=True)  # type: ignore[arg-type]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_memory_embeddings_id", "id"),)


class MemoryAccessBindingORM(Base):
    """ACL binding for memory — who can access a memory (Phase B §27.7/27.8)."""

    __tablename__ = "memory_access_bindings"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    memory_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    principal_type: Mapped[str] = mapped_column(Text, nullable=False)
    principal_id: Mapped[str] = mapped_column(Text, nullable=False)
    permission: Mapped[str] = mapped_column(Text, nullable=False, default="read")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_memory_access_bindings_memory", "memory_id"),
        Index("ix_memory_access_bindings_principal", "principal_type", "principal_id"),
        Index("ix_memory_access_bindings_tenant_principal", "tenant_id", "principal_type", "principal_id"),
    )


class AdminStateORM(Base):
    """Admin Web UI persistent state — generic key/value store for §27.3."""

    __tablename__ = "admin_state"

    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[dict | list | None] = mapped_column(GenericJSON, nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(256), nullable=True)


# ── v1.6 §22 Admin persistence (admin_users, infra_services, user_mappings) ──
# sqlite-compat: Text PKs, GenericJSON extra, no NOW() / server_default


class AdminUserORM(Base):
    """Admin infra user — mirrors admin-console/backend/auth.py AdminUser."""

    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    hashed_password: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    extra: Mapped[dict | None] = mapped_column(GenericJSON, nullable=True)

    __table_args__ = (Index("ix_admin_users_email", "email"),)


class AdminInfraServiceORM(Base):
    """Infra service registry — mirrors admin-console/backend/infra.py InfraService."""

    __tablename__ = "admin_infra_services"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    health_path: Mapped[str] = mapped_column(Text, nullable=False, default="/health")
    expected_status: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=200)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    latency_ms: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    last_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra: Mapped[dict | None] = mapped_column(GenericJSON, nullable=True)


class AdminUserMappingORM(Base):
    """Mattermost -> employee/agent mapping — mirrors admin-console/backend/user_mappings.py."""

    __tablename__ = "admin_user_mappings"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    mm_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    mm_username: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    # task spec column + persistence compat column
    employee_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    employee_principal: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict | None] = mapped_column(GenericJSON, nullable=True)

    __table_args__ = (
        Index("ix_admin_user_mappings_mm_user_id", "mm_user_id"),
        Index("ix_admin_user_mappings_mm_username", "mm_username"),
    )


class AdminLLMProviderORM(Base):
    """LLM provider registry — mirrors admin-console/backend/llm_providers.py LLMProvider.

    Encrypted api_key stored as Fernet ciphertext (Text) or external secret_ref.
    """

    __tablename__ = "admin_llm_providers"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Encrypted api key (Fernet string) — masked on read
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    vault_backend: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_test_latency_ms: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    extra: Mapped[dict | None] = mapped_column(GenericJSON, nullable=True)

    __table_args__ = (Index("ix_admin_llm_providers_provider", "provider"),)


class AdminLLMQuotaORM(Base):
    """Tenant LLM quota — daily + per-minute limits (v1.6.4 §16.4)."""

    __tablename__ = "admin_llm_quotas"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    per_minute_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    used_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    extra: Mapped[dict | None] = mapped_column(GenericJSON, nullable=True)


class AdminLlmUsageORM(Base):
    """Tenant LLM usage — cost/latency/token tracking (v1.6.4 §16.4.1)."""

    __tablename__ = "admin_llm_usage"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="success")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        Index("ix_admin_llm_usage_tenant", "tenant_id"),
        Index("ix_admin_llm_usage_created", "created_at"),
        Index("ix_admin_llm_usage_tenant_created", "tenant_id", "created_at"),
    )


class AdminSettingORM(Base):
    """Generic admin settings key/value — used for runtime_mode (hermes vs llm)."""

    __tablename__ = "admin_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict | None] = mapped_column(GenericJSON, nullable=True)

