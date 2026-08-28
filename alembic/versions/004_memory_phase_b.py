"""Memory Phase B — missing columns + memory_embeddings + memory_access_bindings

Revision ID: 004_memory_phase_b
Revises: 003_admin_persistence
Create Date: 2026-08-28

v1.6 §27.5 missing columns for memories:
  namespace, owner_type, owner_id, memory_type, summary, classification,
  source_resource_type, source_acl_version, source_delegation_id,
  retention_policy, expires_at, invalidated_at, invalidation_reason

Plus new tables:
- memory_embeddings: id FK memories.id PK, embedding Vector(1536)/Text, created_at
- memory_access_bindings: id PK, tenant_id, memory_id FK memories.id, principal_type/id, permission, created_at, expires_at

sqlite compat: nullable True for add_column, Text for enums, no pg-only server_default.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "004_memory_phase_b"
down_revision = "003_admin_persistence"
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
    # ── add missing columns to memories (all nullable for sqlite compat) ──
    op.add_column("memories", sa.Column("namespace", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("owner_type", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("owner_id", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("memory_type", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("classification", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("source_resource_type", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("source_acl_version", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("source_delegation_id", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("retention_policy", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("memories", sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("memories", sa.Column("invalidation_reason", sa.Text(), nullable=True))

    # ── indexes for new columns ──
    op.create_index("ix_memories_namespace", "memories", ["namespace"])
    op.create_index("ix_memories_owner", "memories", ["owner_type", "owner_id"])
    op.create_index("ix_memories_classification", "memories", ["classification"])
    op.create_index("ix_memories_expires_at", "memories", ["expires_at"])
    op.create_index("ix_memories_invalidated_at", "memories", ["invalidated_at"])

    # ── memory_embeddings ────────────────────────────────────────────
    op.create_table(
        "memory_embeddings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("embedding", _embedding_type(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["id"], ["memories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_embeddings_id", "memory_embeddings", ["id"])

    # ── memory_access_bindings ───────────────────────────────────────
    op.create_table(
        "memory_access_bindings",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column("principal_type", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("permission", sa.Text(), nullable=False, server_default="read"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_access_bindings_tenant_id", "memory_access_bindings", ["tenant_id"])
    op.create_index("ix_memory_access_bindings_memory_id", "memory_access_bindings", ["memory_id"])
    op.create_index("ix_memory_access_bindings_memory", "memory_access_bindings", ["memory_id"])
    op.create_index("ix_memory_access_bindings_principal", "memory_access_bindings", ["principal_type", "principal_id"])
    op.create_index(
        "ix_memory_access_bindings_tenant_principal",
        "memory_access_bindings",
        ["tenant_id", "principal_type", "principal_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_access_bindings_tenant_principal", table_name="memory_access_bindings")
    op.drop_index("ix_memory_access_bindings_principal", table_name="memory_access_bindings")
    op.drop_index("ix_memory_access_bindings_memory", table_name="memory_access_bindings")
    op.drop_index("ix_memory_access_bindings_memory_id", table_name="memory_access_bindings")
    op.drop_index("ix_memory_access_bindings_tenant_id", table_name="memory_access_bindings")
    op.drop_table("memory_access_bindings")

    op.drop_index("ix_memory_embeddings_id", table_name="memory_embeddings")
    op.drop_table("memory_embeddings")

    op.drop_index("ix_memories_invalidated_at", table_name="memories")
    op.drop_index("ix_memories_expires_at", table_name="memories")
    op.drop_index("ix_memories_classification", table_name="memories")
    op.drop_index("ix_memories_owner", table_name="memories")
    op.drop_index("ix_memories_namespace", table_name="memories")

    op.drop_column("memories", "invalidation_reason")
    op.drop_column("memories", "invalidated_at")
    op.drop_column("memories", "expires_at")
    op.drop_column("memories", "retention_policy")
    op.drop_column("memories", "source_delegation_id")
    op.drop_column("memories", "source_acl_version")
    op.drop_column("memories", "source_resource_type")
    op.drop_column("memories", "classification")
    op.drop_column("memories", "summary")
    op.drop_column("memories", "memory_type")
    op.drop_column("memories", "owner_id")
    op.drop_column("memories", "owner_type")
    op.drop_column("memories", "namespace")
