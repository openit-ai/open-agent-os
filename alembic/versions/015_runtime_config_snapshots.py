"""Runtime Config Plane — durable canonical storage (Stage-2).

Revision ID: 015_runtime_config_snapshots
Revises: 014_adaptive_profile
Create Date: 2026-08-31

- Creates admin_runtime_config_snapshots (tenant_id TEXT NOT NULL, version INTEGER NOT NULL,
  snapshot_json TEXT NOT NULL, signature TEXT NOT NULL, config_hash TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, parent_version INTEGER nullable,
  rollback_from INTEGER nullable, extra JSON nullable)
  PrimaryKey: (tenant_id, version) via composite PK + id convenience (tenant:version) uniqueness
  Uses composite PK for optimistic version enforcement.
- Creates admin_runtime_config_published (tenant_id TEXT PK, published_version INTEGER NOT NULL,
  config_hash TEXT nullable, updated_at TIMESTAMPTZ NOT NULL, updated_by TEXT NOT NULL)
- Creates admin_runtime_config_applied (tenant_id TEXT PK, applied_version INTEGER nullable,
  config_hash TEXT nullable, applied_at TIMESTAMPTZ nullable, applied_by TEXT nullable,
  process_identity TEXT nullable, error TEXT nullable, updated_at TIMESTAMPTZ NOT NULL)
- Indexes: snapshots tenant+created_at, published tenant, applied tenant
- Idempotent: skips creation if table exists (covers manually created tables).
- PostgreSQL + SQLite compatible (Text PKs, DateTime(timezone=True), Generic JSON).
- Downgrade drops tables if exist (non-destructive to other tables).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "015_runtime_config_snapshots"
down_revision = "014_adaptive_profile"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        return table in insp.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    # ── snapshots ──
    if not _has_table("admin_runtime_config_snapshots"):
        op.create_table(
            "admin_runtime_config_snapshots",
            sa.Column("tenant_id", sa.Text(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("snapshot_json", sa.Text(), nullable=False),
            sa.Column("signature", sa.Text(), nullable=False),
            sa.Column("config_hash", sa.Text(), nullable=False),
            sa.Column("created_by", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("parent_version", sa.Integer(), nullable=True),
            sa.Column("rollback_from", sa.Integer(), nullable=True),
            sa.Column("extra", sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint("tenant_id", "version"),
        )
        try:
            op.create_index("ix_rc_snapshots_tenant_created", "admin_runtime_config_snapshots", ["tenant_id", "created_at"])
        except Exception:
            pass
        try:
            op.create_index("ix_rc_snapshots_signature", "admin_runtime_config_snapshots", ["signature"])
        except Exception:
            pass
    else:
        # ensure indexes even if table pre-existed via ensure_admin_tables
        try:
            insp = sa.inspect(op.get_bind())
            idxs = {ix["name"] for ix in insp.get_indexes("admin_runtime_config_snapshots")}
            if "ix_rc_snapshots_tenant_created" not in idxs:
                op.create_index("ix_rc_snapshots_tenant_created", "admin_runtime_config_snapshots", ["tenant_id", "created_at"])
        except Exception:
            pass
        # ensure new columns exist (migration from stage-1 admin_settings mirror without tables)
        try:
            insp = sa.inspect(op.get_bind())
            cols = {c["name"] for c in insp.get_columns("admin_runtime_config_snapshots")}
            if "config_hash" not in cols:
                op.add_column("admin_runtime_config_snapshots", sa.Column("config_hash", sa.Text(), nullable=True))
                # backfill with placeholder for existing rows (if any)
                try:
                    op.execute(sa.text("UPDATE admin_runtime_config_snapshots SET config_hash='backfill' WHERE config_hash IS NULL"))
                except Exception:
                    pass
            if "extra" not in cols:
                op.add_column("admin_runtime_config_snapshots", sa.Column("extra", sa.JSON(), nullable=True))
        except Exception:
            pass

    # ── published pointer ──
    if not _has_table("admin_runtime_config_published"):
        op.create_table(
            "admin_runtime_config_published",
            sa.Column("tenant_id", sa.Text(), nullable=False),
            sa.Column("published_version", sa.Integer(), nullable=False),
            sa.Column("config_hash", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_by", sa.Text(), nullable=False),
            sa.Column("extra", sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint("tenant_id"),
        )
    else:
        try:
            insp = sa.inspect(op.get_bind())
            cols = {c["name"] for c in insp.get_columns("admin_runtime_config_published")}
            if "config_hash" not in cols:
                op.add_column("admin_runtime_config_published", sa.Column("config_hash", sa.Text(), nullable=True))
        except Exception:
            pass

    # ── applied status (Control Plane durable) ──
    if not _has_table("admin_runtime_config_applied"):
        op.create_table(
            "admin_runtime_config_applied",
            sa.Column("tenant_id", sa.Text(), nullable=False),
            sa.Column("applied_version", sa.Integer(), nullable=True),
            sa.Column("config_hash", sa.Text(), nullable=True),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("applied_by", sa.Text(), nullable=True),
            sa.Column("process_identity", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("tenant_id"),
        )
        try:
            op.create_index("ix_rc_applied_updated", "admin_runtime_config_applied", ["updated_at"])
        except Exception:
            pass


def downgrade() -> None:
    for t in ["admin_runtime_config_applied", "admin_runtime_config_published", "admin_runtime_config_snapshots"]:
        try:
            if _has_table(t):
                op.drop_table(t)
        except Exception:
            pass
