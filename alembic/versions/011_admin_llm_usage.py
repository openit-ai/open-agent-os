"""LLM usage tracking — admin_llm_usage

Revision ID: 011_admin_llm_usage
Revises: 010_tenant_llm_quota
Create Date: 2026-08-29
- admin_llm_usage: id PK TEXT, tenant_id TEXT indexed, provider TEXT nullable,
  model TEXT nullable, prompt_tokens INT default 0, completion_tokens INT default 0,
  total_tokens INT default 0, cost_usd FLOAT default 0, latency_ms FLOAT default 0,
  status TEXT default success, error TEXT nullable, created_at TIMESTAMPTZ indexed
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "011_admin_llm_usage"
down_revision = "010_tenant_llm_quota"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        return table in insp.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    if not _has_table("admin_llm_usage"):
        op.create_table(
            "admin_llm_usage",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("tenant_id", sa.Text(), nullable=False),
            sa.Column("provider", sa.Text(), nullable=True),
            sa.Column("model", sa.Text(), nullable=True),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("cost_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
            sa.Column("latency_ms", sa.Float(), nullable=False, server_default=sa.text("0")),
            sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'success'")),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        try:
            op.create_index("ix_admin_llm_usage_tenant", "admin_llm_usage", ["tenant_id"])
        except Exception:
            pass
        try:
            op.create_index("ix_admin_llm_usage_created", "admin_llm_usage", ["created_at"])
        except Exception:
            pass
        try:
            op.create_index("ix_admin_llm_usage_tenant_created", "admin_llm_usage", ["tenant_id", "created_at"])
        except Exception:
            pass


def downgrade() -> None:
    try:
        if _has_table("admin_llm_usage"):
            try:
                op.drop_index("ix_admin_llm_usage_tenant_created", table_name="admin_llm_usage")
            except Exception:
                pass
            try:
                op.drop_index("ix_admin_llm_usage_created", table_name="admin_llm_usage")
            except Exception:
                pass
            try:
                op.drop_index("ix_admin_llm_usage_tenant", table_name="admin_llm_usage")
            except Exception:
                pass
            op.drop_table("admin_llm_usage")
    except Exception:
        pass
