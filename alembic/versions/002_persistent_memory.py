"""persistent memory: memories, memory_sources, admin_state (v1.6 §27, pgvector ready)

Revision ID: 002_persistent_memory
Revises: 001_initial
Create Date: 2026-08-28

- memories: id, tenant_id, user_id, agent_id, kind, content, embedding VECTOR(1536) nullable, source_ids JSON, created_at, updated_at
- memory_sources: provenance FK to memories
- admin_state: Admin Web UI persistent KV

pgvector: uses Vector(1536) when pgvector is installed & target is Postgres;
falls back to Text/JSON for SQLite (pytest compat).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "002_persistent_memory"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def _embedding_type():
    """Return pgvector Vector(1536) if available, else Text fallback for SQLite."""
    try:
        from pgvector.sqlalchemy import Vector  # type: ignore

        return Vector(1536)
    except Exception:
        return sa.Text()


def upgrade() -> None:
    # Enable pgvector extension on Postgres (no-op on SQLite)
    try:
        # only succeeds on Postgres; on SQLite it will raise and we ignore
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception:
        pass

    # ── memories ───────────────────────────────────────────────────────
    op.create_table(
        "memories",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column("agent_id", sa.String(length=256), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", _embedding_type(), nullable=True),
        sa.Column("source_ids", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memories_tenant_id", "memories", ["tenant_id"])
    op.create_index("ix_memories_user_id", "memories", ["user_id"])
    op.create_index("ix_memories_agent_id", "memories", ["agent_id"])
    op.create_index("ix_memories_tenant_user", "memories", ["tenant_id", "user_id"])
    op.create_index("ix_memories_tenant_agent", "memories", ["tenant_id", "agent_id"])

    # ── memory_sources ─────────────────────────────────────────────────
    op.create_table(
        "memory_sources",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=256), nullable=True),
        sa.Column("source_uri", sa.String(length=512), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_sources_tenant_id", "memory_sources", ["tenant_id"])
    op.create_index("ix_memory_sources_memory_id", "memory_sources", ["memory_id"])
    op.create_index("ix_memory_sources_memory", "memory_sources", ["memory_id"])

    # ── admin_state ────────────────────────────────────────────────────
    op.create_table(
        "admin_state",
        sa.Column("key", sa.String(length=256), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=256), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index("ix_admin_state_category", "admin_state", ["category"])


def downgrade() -> None:
    op.drop_table("admin_state")
    op.drop_table("memory_sources")
    op.drop_table("memories")
    # pgvector extension is kept (shared)
