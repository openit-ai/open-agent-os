"""Add display_name/avatar_url to admin_user_mappings (ORM drift fix).

Revision ID: 016_admin_user_mappings_display_avatar
Revises: 015_runtime_config_snapshots
Create Date: 2026-08-31

ORM AdminUserMappingORM declares display_name (Text nullable) and avatar_url
(Text nullable) but 003_admin_persistence did not create them. This migration
adds them idempotently, preserving existing rows (nullable, no server_default).

Idempotent: checks columns before adding; downgrade drops if present.
SQLite + PostgreSQL compatible (sa.Text, sa.JSON generic).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "016_admin_user_mappings_display_avatar"
down_revision = "015_runtime_config_snapshots"
branch_labels = None
depends_on = None


def _get_columns(table: str) -> set[str]:
    try:
        insp = sa.inspect(op.get_bind())
        cols = insp.get_columns(table)
        return {c["name"] for c in cols}
    except Exception:
        return set()


def _has_table(table: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        return table in insp.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    if not _has_table("admin_user_mappings"):
        return
    cols = _get_columns("admin_user_mappings")
    if "display_name" not in cols:
        try:
            op.add_column("admin_user_mappings", sa.Column("display_name", sa.Text(), nullable=True))
        except Exception:
            pass
    if "avatar_url" not in cols:
        try:
            op.add_column("admin_user_mappings", sa.Column("avatar_url", sa.Text(), nullable=True))
        except Exception:
            pass


def downgrade() -> None:
    if not _has_table("admin_user_mappings"):
        return
    cols = _get_columns("admin_user_mappings")
    for col in ("avatar_url", "display_name"):
        if col in cols:
            try:
                with op.batch_alter_table("admin_user_mappings") as batch:
                    batch.drop_column(col)
            except Exception:
                try:
                    op.drop_column("admin_user_mappings", col)
                except Exception:
                    pass
