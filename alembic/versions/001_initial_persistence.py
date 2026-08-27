"""initial persistence: delegations, bindings, approvals, audit, sessions, vault

Revision ID: 001_initial
Revises: 
Create Date: 2026-08-27

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delegations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column("agent_id", sa.String(length=256), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_delegations_user_id", "delegations", ["user_id"])
    op.create_index("ix_delegations_user_provider", "delegations", ["user_id", "provider"])

    op.create_table(
        "credential_bindings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("delegation_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("secret_ref", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["delegation_id"], ["delegations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("secret_ref"),
    )
    op.create_index("ix_credential_bindings_delegation_id", "credential_bindings", ["delegation_id"])

    op.create_table(
        "approval_requests",
        sa.Column("approval_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column("agent_id", sa.String(length=256), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("risk", sa.String(length=16), nullable=False),
        sa.Column("request_hash", sa.String(length=128), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=256), nullable=True),
        sa.Column("group_id", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("approval_id"),
        sa.UniqueConstraint("nonce"),
    )
    op.create_index("ix_approval_requests_user_id", "approval_requests", ["user_id"])

    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=True),
        sa.Column("agent_id", sa.String(length=256), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=True),
        sa.Column("decision", sa.String(length=32), nullable=True),
        sa.Column("policy_version", sa.String(length=32), nullable=True),
        sa.Column("delegation_id", sa.String(length=64), nullable=True),
        sa.Column("credential_binding_id", sa.String(length=64), nullable=True),
        sa.Column("tool_name", sa.String(length=128), nullable=True),
        sa.Column("parameters_hash", sa.String(length=128), nullable=True),
        sa.Column("result_hash", sa.String(length=128), nullable=True),
        sa.Column("previous_hash", sa.String(length=128), nullable=True),
        sa.Column("event_hash", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index("ix_audit_events_timestamp", "audit_events", ["timestamp"])
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_session_id", "audit_events", ["session_id"])
    op.create_index("ix_audit_tenant_time", "audit_events", ["tenant_id", "timestamp"])

    op.create_table(
        "session_records",
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column("agent_id", sa.String(length=256), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("security_domain", sa.String(length=32), nullable=False),
        sa.Column("hermes_worker", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prompt_history", sa.JSON(), nullable=False),
        sa.Column("stream_events", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index("ix_session_records_tenant_id", "session_records", ["tenant_id"])
    op.create_index("ix_session_records_user_id", "session_records", ["user_id"])
    op.create_index("ix_session_user", "session_records", ["user_id", "tenant_id"])

    op.create_table(
        "vault_credentials",
        sa.Column("secret_ref", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column("owner_agent_id", sa.String(length=256), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=256), nullable=False),
        sa.Column("encrypted_token", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("secret_ref"),
    )
    op.create_index("ix_vault_credentials_user_id", "vault_credentials", ["user_id"])


def downgrade() -> None:
    op.drop_table("vault_credentials")
    op.drop_table("session_records")
    op.drop_table("audit_events")
    op.drop_table("approval_requests")
    op.drop_table("credential_bindings")
    op.drop_table("delegations")
