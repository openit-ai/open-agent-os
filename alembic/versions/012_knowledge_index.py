"""Enterprise Knowledge Index (v1.7.1 §0.4.1) — persistent table knowledge_index

Revision ID: 012_knowledge_index
Revises: 011_admin_llm_usage
Create Date: 2026-08-29

Fields per architecture-v1.7.2 §0.4.1:
 index_id, source_system, source_resource_id, source_uri, tenant_id,
 group_id/agent_id, chunk_id, chunk_text, embedding VECTOR(1536) nullable
 (SQLite fallback to Text/JSON), content_hash, source_updated_at, indexed_at,
 acl_version, classification, retention_policy, provenance
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "012_knowledge_index"
down_revision = "011_admin_llm_usage"
branch_labels = None
depends_on = None


def _embedding_type():
    try:
        from pgvector.sqlalchemy import Vector  # type: ignore

        return Vector(1536)
    except Exception:
        return sa.Text()


def _has_table(table: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        return table in insp.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    if _has_table("knowledge_index"):
        return
    # pgvector extension (no-op on SQLite)
    try:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception:
        pass

    op.create_table(
        "knowledge_index",
        sa.Column("index_id", sa.String(length=64), nullable=False),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("source_resource_id", sa.String(length=256), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("group_id", sa.Text(), nullable=True),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("chunk_id", sa.String(length=64), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("embedding", _embedding_type(), nullable=True),
        sa.Column("content_hash", sa.String(length=128), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acl_version", sa.String(length=64), nullable=True),
        sa.Column("classification", sa.Text(), nullable=True),
        sa.Column("retention_policy", sa.Text(), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("index_id"),
    )
    for idx, cols in [
        ("ix_ki_tenant_id", ["tenant_id"]),
        ("ix_ki_source_resource_id", ["source_resource_id"]),
        ("ix_ki_source_system", ["source_system"]),
        ("ix_ki_group_id", ["group_id"]),
        ("ix_ki_agent_id", ["agent_id"]),
        ("ix_ki_tenant_group", ["tenant_id", "group_id"]),
        ("ix_ki_tenant_agent", ["tenant_id", "agent_id"]),
        ("ix_ki_classification", ["classification"]),
        ("ix_ki_indexed_at", ["indexed_at"]),
    ]:
        try:
            op.create_index(idx, "knowledge_index", cols)
        except Exception:
            pass


def downgrade() -> None:
    try:
        if _has_table("knowledge_index"):
            for idx in [
                "ix_ki_indexed_at",
                "ix_ki_classification",
                "ix_ki_tenant_agent",
                "ix_ki_tenant_group",
                "ix_ki_agent_id",
                "ix_ki_group_id",
                "ix_ki_source_system",
                "ix_ki_source_resource_id",
                "ix_ki_tenant_id",
            ]:
                try:
                    op.drop_index(idx, table_name="knowledge_index")
                except Exception:
                    pass
            op.drop_table("knowledge_index")
    except Exception:
        pass
