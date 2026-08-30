"""Adaptive Profile Engine — MVP tables (v1.7.2 §16.12).

Revision ID: 014_adaptive_profile
Revises: 013_admin_policy_versions
Create Date: 2026-08-30

Tables (all tenant+user isolated, idempotent content_hash):
  user_profiles(user_id, tenant_id, profile_version, status, evidence_count,
                overall_confidence, created_at, updated_at, extra)
  trait_scores(user_id, tenant_id, trait_name, global_score, confidence,
               sample_count, last_updated)
  task_trait_scores(user_id, tenant_id, task_type, trait_name, score,
                    confidence, sample_count, last_updated)
  profile_evidence(evidence_id, user_id, tenant_id, conversation_id,
                   message_id, task_type, trait, direction, strength,
                   source_type, confidence, observed_at, content_hash UNIQUE)
  explicit_preferences(preference_id, user_id, tenant_id, scope, task_type,
                       key, value, priority, created_at, updated_at)

SQLite + PostgreSQL compatible. Idempotent upgrade/downgrade.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "014_adaptive_profile"
down_revision = "013_admin_policy_versions"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        return table in insp.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    if not _has_table("user_profiles"):
        op.create_table(
            "user_profiles",
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("tenant_id", sa.Text(), nullable=False),
            sa.Column("profile_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.Text(), nullable=False, server_default="active"),
            sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("overall_confidence", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("extra", sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint("user_id", "tenant_id"),
        )
        try:
            op.create_index("ix_user_profiles_tenant", "user_profiles", ["tenant_id"])
        except Exception:
            pass
        try:
            op.create_index("ix_user_profiles_updated", "user_profiles", ["updated_at"])
        except Exception:
            pass

    if not _has_table("trait_scores"):
        op.create_table(
            "trait_scores",
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("tenant_id", sa.Text(), nullable=False),
            sa.Column("trait_name", sa.Text(), nullable=False),
            sa.Column("global_score", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_updated", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("user_id", "tenant_id", "trait_name"),
        )
        for idx, cols in [
            ("ix_trait_scores_tenant_user", ["tenant_id", "user_id"]),
            ("ix_trait_scores_trait", ["trait_name"]),
        ]:
            try:
                op.create_index(idx, "trait_scores", cols)
            except Exception:
                pass

    if not _has_table("task_trait_scores"):
        op.create_table(
            "task_trait_scores",
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("tenant_id", sa.Text(), nullable=False),
            sa.Column("task_type", sa.Text(), nullable=False),
            sa.Column("trait_name", sa.Text(), nullable=False),
            sa.Column("score", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_updated", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("user_id", "tenant_id", "task_type", "trait_name"),
        )
        for idx, cols in [
            ("ix_task_trait_tenant_user", ["tenant_id", "user_id"]),
            ("ix_task_trait_task", ["task_type"]),
        ]:
            try:
                op.create_index(idx, "task_trait_scores", cols)
            except Exception:
                pass

    if not _has_table("profile_evidence"):
        op.create_table(
            "profile_evidence",
            sa.Column("evidence_id", sa.Text(), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("tenant_id", sa.Text(), nullable=False),
            sa.Column("conversation_id", sa.Text(), nullable=True),
            sa.Column("message_id", sa.Text(), nullable=True),
            sa.Column("task_type", sa.Text(), nullable=True),
            sa.Column("trait", sa.Text(), nullable=False),
            sa.Column("direction", sa.Integer(), nullable=False),
            sa.Column("strength", sa.Float(), nullable=False),
            sa.Column("source_type", sa.Text(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("content_hash", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("evidence_id"),
        )
        for idx, cols, uniq in [
            ("ix_profile_evidence_tenant_user", ["tenant_id", "user_id"], False),
            ("ix_profile_evidence_content_hash", ["content_hash"], True),
            ("ix_profile_evidence_trait", ["trait"], False),
            ("ix_profile_evidence_task", ["task_type"], False),
            ("ix_profile_evidence_observed", ["observed_at"], False),
        ]:
            try:
                op.create_index(idx, "profile_evidence", cols, unique=uniq)
            except Exception:
                pass

    if not _has_table("explicit_preferences"):
        op.create_table(
            "explicit_preferences",
            sa.Column("preference_id", sa.Text(), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("tenant_id", sa.Text(), nullable=False),
            sa.Column("scope", sa.Text(), nullable=False, server_default="global"),
            sa.Column("task_type", sa.Text(), nullable=True),
            sa.Column("key", sa.Text(), nullable=False),
            sa.Column("value", sa.Text(), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("preference_id"),
        )
        for idx, cols in [
            ("ix_explicit_pref_tenant_user", ["tenant_id", "user_id"]),
            ("ix_explicit_pref_key", ["key"]),
            ("ix_explicit_pref_scope", ["scope"]),
            ("ix_explicit_pref_tenant_user_key", ["tenant_id", "user_id", "key"]),
        ]:
            try:
                op.create_index(idx, "explicit_preferences", cols)
            except Exception:
                pass


def downgrade() -> None:
    for t in ["explicit_preferences", "profile_evidence", "task_trait_scores", "trait_scores", "user_profiles"]:
        try:
            if _has_table(t):
                op.drop_table(t)
        except Exception:
            pass
