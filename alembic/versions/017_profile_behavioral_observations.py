"""Behavioral Profile observations, aggregates, projections, and settings.

Revision ID: 017_profile_behavioral
Revises: 016_user_map_avatar
"""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa

revision = "017_profile_behavioral"
down_revision = "016_user_map_avatar"
branch_labels = None
depends_on = None


def _has(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has("profile_observations"):
        op.create_table(
            "profile_observations",
            sa.Column("observation_id", sa.Text(), primary_key=True),
            sa.Column("tenant_id", sa.Text(), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("agent_id", sa.Text(), nullable=True),
            sa.Column("session_id", sa.Text(), nullable=True),
            sa.Column("conversation_id", sa.Text(), nullable=True),
            sa.Column("message_id", sa.Text(), nullable=True),
            sa.Column("task_type", sa.Text(), nullable=False),
            sa.Column("feature_name", sa.Text(), nullable=False),
            sa.Column("normalized_value", sa.Float(), nullable=False),
            sa.Column("source_type", sa.Text(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("source_ref_hash", sa.Text(), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_profile_obs_owner_time", "profile_observations", ["tenant_id", "user_id", "observed_at"])
        op.create_index("ix_profile_obs_idempotency", "profile_observations", ["source_ref_hash"], unique=True)
    if not _has("profile_feature_aggregates"):
        op.create_table(
            "profile_feature_aggregates",
            sa.Column("aggregate_id", sa.Text(), primary_key=True),
            sa.Column("tenant_id", sa.Text(), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("task_type", sa.Text(), nullable=False),
            sa.Column("feature_name", sa.Text(), nullable=False),
            sa.Column("window", sa.Text(), nullable=False),
            sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("mean", sa.Float(), nullable=False, server_default="0"),
            sa.Column("variance", sa.Float(), nullable=False, server_default="0"),
            sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_profile_agg_owner", "profile_feature_aggregates", ["tenant_id", "user_id"])
    if not _has("profile_projections"):
        op.create_table(
            "profile_projections",
            sa.Column("projection_id", sa.Text(), primary_key=True),
            sa.Column("tenant_id", sa.Text(), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("projection_version", sa.Text(), nullable=False),
            sa.Column("result_json", sa.JSON(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("input_profile_version", sa.Integer(), nullable=False),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_profile_projection_owner", "profile_projections", ["tenant_id", "user_id"])
    if not _has("profile_settings"):
        op.create_table(
            "profile_settings",
            sa.Column("tenant_id", sa.Text(), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("learning_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("retention_days", sa.Integer(), nullable=False, server_default="365"),
            sa.Column("projection_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("consent_updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("tenant_id", "user_id"),
        )


def downgrade() -> None:
    for name in ("profile_settings", "profile_projections", "profile_feature_aggregates", "profile_observations"):
        if _has(name):
            op.drop_table(name)
