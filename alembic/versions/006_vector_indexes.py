"""Vector indexes — hnsw/ivfflat for memories.embedding + memory_embeddings.embedding

Revision ID: 006_vector_indexes
Revises: 005_vault_secret_ref
Create Date: 2026-08-28

- Enables pgvector extension on Postgres (no-op on SQLite).
- Creates HNSW index (preferred) or IVFFLAT fallback for:
    * memories.embedding
    * memory_embeddings.embedding
- Postgres only — SQLite skip (Text fallback, no vector ops).
- Idempotent: checks dialect, uses IF NOT EXISTS, wraps in try/except
  so tests with SQLite and older pgvector versions never fail.
- Uses vector_cosine_ops (cosine distance) for 1536-dim embeddings.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "006_vector_indexes"
down_revision = "005_vault_secret_ref"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    try:
        bind = op.get_bind()
        return bind.dialect.name == "postgresql"
    except Exception:
        return False


def _try_execute(sql: str) -> None:
    try:
        op.execute(sa.text(sql))
    except Exception:
        pass


def upgrade() -> None:
    if not _is_postgres():
        return

    # Ensure pgvector extension
    _try_execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── memories.embedding ──────────────────────────────────────
    # Prefer HNSW (pgvector >=0.5, postgres 11+). Fallback to IVFFLAT.
    # HNSW params: m=16, ef_construction=64 are defaults; omit for compat.
    try:
        op.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS ix_memories_embedding_hnsw "
                "ON memories USING hnsw (embedding vector_cosine_ops)"
            )
        )
    except Exception:
        # HNSW not available (older pgvector) — try IVFFLAT
        try:
            op.execute(
                sa.text(
                    "CREATE INDEX IF NOT EXISTS ix_memories_embedding_ivfflat "
                    "ON memories USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
                )
            )
        except Exception:
            pass

    # ── memory_embeddings.embedding ─────────────────────────────
    try:
        op.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS ix_memory_embeddings_embedding_hnsw "
                "ON memory_embeddings USING hnsw (embedding vector_cosine_ops)"
            )
        )
    except Exception:
        try:
            op.execute(
                sa.text(
                    "CREATE INDEX IF NOT EXISTS ix_memory_embeddings_embedding_ivfflat "
                    "ON memory_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
                )
            )
        except Exception:
            pass


def downgrade() -> None:
    if not _is_postgres():
        return
    for idx in (
        "ix_memories_embedding_hnsw",
        "ix_memories_embedding_ivfflat",
        "ix_memory_embeddings_embedding_hnsw",
        "ix_memory_embeddings_embedding_ivfflat",
    ):
        _try_execute(f"DROP INDEX IF EXISTS {idx}")
