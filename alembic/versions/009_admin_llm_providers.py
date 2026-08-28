"""LLM providers + admin_settings for runtime_mode

Revision ID: 009_admin_llm_providers
Revises: 008_approval_nonces
Create Date: 2026-08-28

- admin_llm_providers: id PK TEXT, provider TEXT, name TEXT, encrypted_api_key TEXT nullable,
  secret_ref TEXT nullable, vault_backend TEXT nullable, base_url TEXT nullable,
  model TEXT nullable, path TEXT nullable, url TEXT nullable,
  enabled BOOLEAN default true, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
  last_test_at TIMESTAMPTZ nullable, last_test_status TEXT nullable,
  last_test_latency_ms FLOAT nullable, extra JSON nullable
- admin_settings: key PK TEXT, value TEXT nullable, updated_at TIMESTAMPTZ,
  updated_by TEXT nullable, extra JSON nullable
  seeded with runtime_mode = hermes

sqlite compat: Text PKs, GenericJSON, no server_default complexities.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "009_admin_llm_providers"
down_revision = "008_approval_nonces"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        return table in insp.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    if not _has_table("admin_llm_providers"):
        op.create_table(
            "admin_llm_providers",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("provider", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False, server_default=""),
            sa.Column("encrypted_api_key", sa.Text(), nullable=True),
            sa.Column("secret_ref", sa.Text(), nullable=True),
            sa.Column("vault_backend", sa.Text(), nullable=True),
            sa.Column("base_url", sa.Text(), nullable=True),
            sa.Column("model", sa.Text(), nullable=True),
            sa.Column("path", sa.Text(), nullable=True),
            sa.Column("url", sa.Text(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_test_status", sa.Text(), nullable=True),
            sa.Column("last_test_latency_ms", sa.Float(), nullable=True),
            sa.Column("extra", sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        try:
            op.create_index("ix_admin_llm_providers_provider", "admin_llm_providers", ["provider"])
        except Exception:
            pass
        try:
            op.create_index("ix_admin_llm_providers_secret_ref", "admin_llm_providers", ["secret_ref"])
        except Exception:
            pass

    if not _has_table("admin_settings"):
        op.create_table(
            "admin_settings",
            sa.Column("key", sa.Text(), nullable=False),
            sa.Column("value", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_by", sa.Text(), nullable=True),
            sa.Column("extra", sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint("key"),
        )
        # seed runtime_mode = hermes
        try:
            from datetime import datetime, timezone
            conn = op.get_bind()
            now = datetime.now(timezone.utc).isoformat()
            # use raw SQL for compat
            conn.execute(sa.text("INSERT OR IGNORE INTO admin_settings (key, value, updated_at) VALUES ('runtime_mode', 'hermes', :now)"), {"now": now})
            # postgres fallback: try INSERT ... ON CONFLICT
            try:
                conn.execute(sa.text("INSERT INTO admin_settings (key, value, updated_at) VALUES ('runtime_mode', 'hermes', :now) ON CONFLICT (key) DO NOTHING"), {"now": now})
            except Exception:
                pass
        except Exception:
            pass


def downgrade() -> None:
    try:
        if _has_table("admin_settings"):
            op.drop_table("admin_settings")
    except Exception:
        pass
    try:
        if _has_table("admin_llm_providers"):
            try:
                op.drop_index("ix_admin_llm_providers_secret_ref", table_name="admin_llm_providers")
            except Exception:
                pass
            try:
                op.drop_index("ix_admin_llm_providers_provider", table_name="admin_llm_providers")
            except Exception:
                pass
            op.drop_table("admin_llm_providers")
    except Exception:
        pass
