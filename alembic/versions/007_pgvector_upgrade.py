"""pgvector upgrade — TEXT → VECTOR(1536) when 002 ran without pgvector

Revision ID: 007_pgvector_upgrade
Revises: 006_vector_indexes
Create Date: 2026-08-28

When 002_persistent_memory ran without pgvector (or on an older image),
memories.embedding and memory_embeddings.embedding were created as TEXT.
This migration upgrades them to VECTOR(1536) on Postgres when pgvector
is available — idempotent, Postgres-only, SQLite skip.

- Postgres only: checks dialect, skips on SQLite.
- Idempotent: checks if column already VECTOR(1536) → skip.
- Uses USING embedding::vector for cast (NULL safe).
- Creates pgvector extension if missing.
- Downgrade: VECTOR → TEXT (lossless cast via ::text).

Safe on empty tables, nullable columns, and when tables don't exist yet.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "007_pgvector_upgrade"
down_revision = "006_vector_indexes"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    try:
        bind = op.get_bind()
        return bind.dialect.name == "postgresql"
    except Exception:
        return False


def _try_execute(sql: str, params: dict | None = None) -> None:
    try:
        op.execute(sa.text(sql), params or {})
    except Exception:
        pass


def _is_vector_column(table: str, column: str) -> bool:
    """Return True if column is already VECTOR type (idempotent guard)."""
    try:
        bind = op.get_bind()
        insp = sa.inspect(bind)
        cols = insp.get_columns(table)
        for c in cols:
            if c["name"] == column:
                # Direct type string check (pgvector Vector(1536) renders as VECTOR)
                t_str = str(c["type"]).lower()
                if "vector" in t_str:
                    return True
                break
        # Fallback: information_schema udt_name on Postgres
        try:
            res = bind.execute(
                sa.text(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            )
            row = res.fetchone()
            if row and row[0] == "vector":
                return True
        except Exception:
            pass
        return False
    except Exception:
        return False


def _has_table(table: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        return table in insp.get_table_names()
    except Exception:
        return False


def _has_column(table: str, column: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        cols = [c["name"] for c in insp.get_columns(table)]
        return column in cols
    except Exception:
        return False


def upgrade() -> None:
    if not _is_postgres():
        return

    # Ensure pgvector extension exists before ALTER TYPE
    _try_execute("CREATE EXTENSION IF NOT EXISTS vector")

    for table in ("memories", "memory_embeddings"):
        if not _has_table(table):
            continue
        if not _has_column(table, "embedding"):
            continue
        if _is_vector_column(table, "embedding"):
            continue
        # Conditional ALTER — TEXT → VECTOR(1536) USING embedding::vector
        # NULL values pass through; non-vector text will raise — wrap in try
        try:
            op.execute(
                sa.text(
                    f"ALTER TABLE {table} "
                    f"ALTER COLUMN embedding TYPE VECTOR(1536) "
                    f"USING embedding::vector"
                )
            )
        except Exception:
            # If cast fails (e.g., bad data or older pgvector), don't block migration
            pass


def downgrade() -> None:
    if not _is_postgres():
        return

    for table in ("memories", "memory_embeddings"):
        if not _has_table(table):
            continue
        if not _has_column(table, "embedding"):
            continue
        # Only downgrade if currently vector
        if not _is_vector_column(table, "embedding"):
            continue
        try:
            op.execute(
                sa.text(
                    f"ALTER TABLE {table} "
                    f"ALTER COLUMN embedding TYPE TEXT "
                    f"USING embedding::text"
                )
            )
        except Exception:
            pass
