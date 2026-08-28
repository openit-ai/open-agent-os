"""Persist Approval nonces — approval_nonces table for replay protection.

Revision ID: 008_approval_nonces
Revises: 007_pgvector_upgrade
Create Date: 2026-08-28

- Creates approval_nonces (nonce TEXT PK, created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL)
- Index on expires_at for TTL cleanup (300s token expiry)
- Idempotent: skips if table already exists (covers Base.metadata.create_all fallback)
- Postgres + SQLite compatible (Generic Text PK, DateTime(timezone=True))
- Downgrade drops table.

Verifies replay protection survives restart via DB query: a separate ApprovalStore
instance must detect already-seen nonce through DB, not just in-memory set.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "008_approval_nonces"
down_revision = "007_pgvector_upgrade"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        return table in insp.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    if _has_table("approval_nonces"):
        return
    op.create_table(
        "approval_nonces",
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("nonce"),
    )
    try:
        op.create_index("ix_approval_nonces_expires_at", "approval_nonces", ["expires_at"])
    except Exception:
        pass


def downgrade() -> None:
    if not _has_table("approval_nonces"):
        return
    try:
        op.drop_index("ix_approval_nonces_expires_at", table_name="approval_nonces")
    except Exception:
        pass
    try:
        op.drop_table("approval_nonces")
    except Exception:
        pass
