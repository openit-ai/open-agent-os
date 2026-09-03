"""Add tenant/source durable Knowledge Index sync checkpoints."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "018_knowledge_sync_checkpoints"
down_revision = "017_profile_behavioral"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("knowledge_sync_checkpoints"):
        return
    op.create_table(
        "knowledge_sync_checkpoints",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("last_sync_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("resource_states", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "source_system"),
    )
    op.create_index(
        "ix_knowledge_sync_checkpoints_updated_at",
        "knowledge_sync_checkpoints",
        ["updated_at"],
    )


def downgrade() -> None:
    if _has_table("knowledge_sync_checkpoints"):
        op.drop_index("ix_knowledge_sync_checkpoints_updated_at", table_name="knowledge_sync_checkpoints")
        op.drop_table("knowledge_sync_checkpoints")
