"""Tenant LLM quota — rate + daily limits

Revision ID: 010_tenant_llm_quota
Revises: 009_admin_llm_providers
Create Date: 2026-08-29

- admin_llm_quotas: tenant_id PK TEXT, daily_limit INT default 100,
  per_minute_limit INT default 10, used_today INT default 0,
  window_start TIMESTAMPTZ, updated_at TIMESTAMPTZ, extra JSON nullable

sqlite compat: Text PK, no server_default complexities.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "010_tenant_llm_quota"
down_revision = "009_admin_llm_providers"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        return table in insp.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    if not _has_table("admin_llm_quotas"):
        op.create_table(
            "admin_llm_quotas",
            sa.Column("tenant_id", sa.Text(), nullable=False),
            sa.Column("daily_limit", sa.Integer(), nullable=False, server_default=sa.text("100")),
            sa.Column("per_minute_limit", sa.Integer(), nullable=False, server_default=sa.text("10")),
            sa.Column("used_today", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("extra", sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint("tenant_id"),
        )


def downgrade() -> None:
    try:
        if _has_table("admin_llm_quotas"):
            op.drop_table("admin_llm_quotas")
    except Exception:
        pass
