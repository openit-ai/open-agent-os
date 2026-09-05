"""Normalize Knowledge Index embeddings to the configured bge-m3 VECTOR(1024).

Revision ID: 019_ki_embedding_1024
Revises: 018_knowledge_sync_checkpoints

The live index was originally created as TEXT because migration 012 selected
the fallback type in the migration process. Production currently uses
Ollama ``bge-m3:latest`` and ``OAOS_EMBED_DIM=1024``. This revision converts
only valid JSON arrays of exactly 1024 finite numbers and refuses to mutate
the table when incompatible rows exist. It is intentionally PostgreSQL-only;
SQLite test schemas keep their Text compatibility.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "019_ki_embedding_1024"
down_revision = "018_knowledge_sync_checkpoints"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _is_vector() -> bool:
    row = op.get_bind().execute(
        sa.text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name='knowledge_index' AND column_name='embedding'"
        )
    ).first()
    return bool(row and row[0] == "vector")


def upgrade() -> None:
    if not _is_postgres():
        return
    bind = op.get_bind()
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    if _is_vector():
        # Existing VECTOR columns are not silently re-dimensioned. A model
        # contract change requires a separately reviewed parallel index.
        return
    # Validate before ALTER so a malformed or mixed-dimension row cannot be
    # silently cast into the new contract. Existing rows from an older
    # embedding contract are retained as NULL and must be re-embedded by the
    # bounded worker before semantic search.
    bad = bind.execute(
        sa.text(
            "SELECT count(*) FROM knowledge_index "
            "WHERE embedding IS NOT NULL AND ("
            "jsonb_typeof(embedding::jsonb) <> 'array' OR "
            "jsonb_array_length(embedding::jsonb) <> 1024)"
        )
    ).scalar_one()
    if int(bad or 0):
        op.execute(
            "ALTER TABLE knowledge_index ALTER COLUMN embedding TYPE VECTOR(1024) "
            "USING CASE WHEN embedding IS NULL OR jsonb_array_length(embedding::jsonb) <> 1024 "
            "THEN NULL ELSE embedding::vector(1024) END"
        )
        return
    op.execute(
        "ALTER TABLE knowledge_index ALTER COLUMN embedding TYPE VECTOR(1024) "
        "USING CASE WHEN embedding IS NULL THEN NULL ELSE embedding::vector(1024) END"
    )


def downgrade() -> None:
    if not _is_postgres() or not _is_vector():
        return
    # Downgrade is lossless for values, but the next upgrade must validate the
    # JSON array contract again. No data is deleted.
    op.execute(
        "ALTER TABLE knowledge_index ALTER COLUMN embedding TYPE TEXT "
        "USING embedding::text"
    )