"""Admin persistence — admin_users, admin_infra_services, admin_user_mappings

Revision ID: 003_admin_persistence
Revises: 002_persistent_memory
Create Date: 2026-08-28

Tables:
- admin_users: id PK TEXT, email UNIQUE TEXT, display_name TEXT, role TEXT,
               hashed_password TEXT, created_at TIMESTAMPTZ, extra JSON
- admin_infra_services: id PK TEXT, name TEXT, display_name TEXT, host TEXT,
               port INTEGER, health_path TEXT, expected_status INTEGER,
               status TEXT, latency_ms DOUBLE, last_check TIMESTAMPTZ, extra JSON
- admin_user_mappings: id PK TEXT, mm_user_id TEXT, mm_username TEXT UNIQUE,
               employee_id TEXT, employee_principal TEXT, agent_id TEXT,
               status TEXT, created_at TIMESTAMPTZ, created_by TEXT, extra JSON

sqlite compat: no NOW(), no server_default with NOW(); use nullable or sa.func.now() at app layer.
All columns use Generic JSON (sa.JSON) for sqlite/postgres compat.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "003_admin_persistence"
down_revision = "002_persistent_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── admin_users ──────────────────────────────────────────────────
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("hashed_password", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_admin_users_email", "admin_users", ["email"])

    # ── admin_infra_services ─────────────────────────────────────────
    op.create_table(
        "admin_infra_services",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("health_path", sa.Text(), nullable=False, server_default="/health"),
        sa.Column("expected_status", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("status", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("last_check", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # ── admin_user_mappings ──────────────────────────────────────────
    op.create_table(
        "admin_user_mappings",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("mm_user_id", sa.Text(), nullable=False),
        sa.Column("mm_username", sa.Text(), nullable=True),
        sa.Column("employee_id", sa.Text(), nullable=True),
        sa.Column("employee_principal", sa.Text(), nullable=True),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mm_username"),
    )
    op.create_index("ix_admin_user_mappings_mm_user_id", "admin_user_mappings", ["mm_user_id"])
    op.create_index("ix_admin_user_mappings_mm_username", "admin_user_mappings", ["mm_username"])


def downgrade() -> None:
    op.drop_index("ix_admin_user_mappings_mm_username", table_name="admin_user_mappings")
    op.drop_index("ix_admin_user_mappings_mm_user_id", table_name="admin_user_mappings")
    op.drop_table("admin_user_mappings")
    op.drop_table("admin_infra_services")
    op.drop_index("ix_admin_users_email", table_name="admin_users")
    op.drop_table("admin_users")
