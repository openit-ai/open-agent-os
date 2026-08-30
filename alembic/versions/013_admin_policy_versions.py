"""Admin policy versions — immutable Draft→Approved→Published history.

Revision ID: 013_admin_policy_versions
Revises: 012_knowledge_index
Create Date: 2026-08-30

- Creates admin_policy_versions (id TEXT PK, tenant_id TEXT NOT NULL,
  bundle_id TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
  status TEXT NOT NULL, rules_json TEXT NOT NULL, created_by TEXT nullable,
  created_at TIMESTAMPTZ NOT NULL, approved_by TEXT nullable,
  approved_at TIMESTAMPTZ nullable, published_at TIMESTAMPTZ nullable,
  parent_version TEXT nullable)
- Indexes: ix_policy_tenant_status (tenant_id, status), ix_policy_bundle (bundle_id, version)
- Idempotent: skips if table already exists (covers manually created table on prod
  where alembic_version was 008_approval_nonces and DDL was executed manually
  via persistence.py/policy.py ensure). Preserves existing data without loss.
  Also ensures indexes exist even when table pre-exists.
- PostgreSQL and SQLite compatible (Generic Text PK, DateTime(timezone=True), sa.JSON/Text via Text).
- Downgrade drops indexes + table if exists (non-destructive to other tables).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "013_admin_policy_versions"
down_revision = "012_knowledge_index"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        return table in insp.get_table_names()
    except Exception:
        return False


def _has_index(table: str, index: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        return any(ix.get("name") == index for ix in insp.get_indexes(table))
    except Exception:
        return False


def upgrade() -> None:
    if _has_table("admin_policy_versions"):
        # Table manually created earlier — ensure indexes exist, preserve data.
        try:
            if not _has_index("admin_policy_versions", "ix_policy_tenant_status"):
                op.create_index("ix_policy_tenant_status", "admin_policy_versions", ["tenant_id", "status"])
        except Exception:
            pass
        try:
            if not _has_index("admin_policy_versions", "ix_policy_bundle"):
                op.create_index("ix_policy_bundle", "admin_policy_versions", ["bundle_id", "version"])
        except Exception:
            pass
        return

    op.create_table(
        "admin_policy_versions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("bundle_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rules_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parent_version", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    try:
        op.create_index("ix_policy_tenant_status", "admin_policy_versions", ["tenant_id", "status"])
    except Exception:
        pass
    try:
        op.create_index("ix_policy_bundle", "admin_policy_versions", ["bundle_id", "version"])
    except Exception:
        pass


def downgrade() -> None:
    if not _has_table("admin_policy_versions"):
        return
    try:
        op.drop_index("ix_policy_bundle", table_name="admin_policy_versions")
    except Exception:
        pass
    try:
        op.drop_index("ix_policy_tenant_status", table_name="admin_policy_versions")
    except Exception:
        pass
    try:
        op.drop_table("admin_policy_versions")
    except Exception:
        pass
