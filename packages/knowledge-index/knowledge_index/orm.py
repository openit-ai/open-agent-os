"""Knowledge Index — persistent schema (v1.7.1 §0.4.1).

Fields: index_id, source_system, source_resource_id, source_uri, tenant_id,
group_id/agent_id, chunk_id, chunk_text, embedding VECTOR(1536) nullable
with SQLite fallback, content_hash, source_updated_at, indexed_at, acl_version,
classification, retention_policy, provenance.
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import String, Text, DateTime, Index
from sqlalchemy import JSON as GenericJSON
from sqlalchemy.orm import Mapped, mapped_column

try:
    from security.models.db import Base  # type: ignore
except Exception:  # fallback when security not on path
    from sqlalchemy.orm import DeclarativeBase

    class Base(DeclarativeBase):  # type: ignore[no-redef]
        pass

# pgvector fallback
try:
    from pgvector.sqlalchemy import Vector as _PgVector  # type: ignore
except Exception:  # pragma: no cover - package absent in lightweight test env
    _PgVector = None  # type: ignore

_VECTOR_1536 = Text  # legacy fallback; migration/runtime contract is 1024


class KnowledgeIndexORM(Base):
    """Derived index for Enterprise Knowledge — not source of truth, but search accelerator."""

    __tablename__ = "knowledge_index"

    index_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_resource_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    group_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_id: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    # The production migration 019 normalizes the live bge-m3 index to
    # VECTOR(1024). Keep SQLite/Text fallback for isolated tests; the model
    # contract is enforced by migration and the provider dimension gate.
    try:
        from pgvector.sqlalchemy import Vector as _PgVectorRuntime  # type: ignore
        _VECTOR_RUNTIME = _PgVectorRuntime(1024)
    except Exception:  # pragma: no cover - SQLite/test fallback
        _VECTOR_RUNTIME = Text
    embedding: Mapped[str | None] = mapped_column(_VECTOR_RUNTIME, nullable=True)  # type: ignore[arg-type]
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acl_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    classification: Mapped[str | None] = mapped_column(Text, nullable=True)
    retention_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[dict | None] = mapped_column(GenericJSON, nullable=True)

    __table_args__ = (
        Index("ix_ki_tenant_id", "tenant_id"),
        Index("ix_ki_source_resource_id", "source_resource_id"),
        Index("ix_ki_source_system", "source_system"),
        Index("ix_ki_group_id", "group_id"),
        Index("ix_ki_agent_id", "agent_id"),
        Index("ix_ki_tenant_group", "tenant_id", "group_id"),
        Index("ix_ki_tenant_agent", "tenant_id", "agent_id"),
        Index("ix_ki_classification", "classification"),
        Index("ix_ki_indexed_at", "indexed_at"),
    )
