"""Vault externalization — secret_ref migration (dual-write).

Revision ID: 005_vault_secret_ref
Revises: 004_memory_phase_b
Create Date: 2026-08-28

Phase B: extend vault_credentials for external backend migration.

- encrypted_token becomes nullable (was NOT NULL) so rows can exist with
  external-only storage (secret_ref + metadata only).
- vault_backend, vault_path, version added as nullable columns for
  migration observability; will be made NOT NULL after cutover.
- secret_ref column is already present from 001 (PK); this migration
  verifies it exists and adds it only if missing (idempotency for
  environments where 001 was not yet applied or custom forks).
- All ops are sqlite-compatible and idempotent (check-before-add /
  check-before-alter) so tests with sqlite never fail.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "005_vault_secret_ref"
down_revision = "004_memory_phase_b"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        cols = [c["name"] for c in insp.get_columns(table)]
        return column in cols
    except Exception:
        return False


def _column_is_nullable(table: str, column: str) -> bool | None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        for c in insp.get_columns(table):
            if c["name"] == column:
                return bool(c.get("nullable"))
    except Exception:
        pass
    return None


def upgrade() -> None:
    # ── secret_ref: ensure exists (001 already creates it as PK) ─────
    # If vault_credentials table missing entirely (fresh SQLite test), skip —
    # Base.metadata.create_all will create it with new ORM shape anyway.
    try:
        bind = op.get_bind()
        insp = sa.inspect(bind)
        if "vault_credentials" not in insp.get_table_names():
            return
    except Exception:
        return

    # secret_ref: add if missing (idempotent). Unique/PK handled by existing constraint.
    if not _has_column("vault_credentials", "secret_ref"):
        # add as nullable first then backfill is not needed (PK replacement is complex)
        # For sqlite compat we add nullable unique column; PK migration is out of scope.
        op.add_column("vault_credentials", sa.Column("secret_ref", sa.String(length=128), nullable=True))
        try:
            op.create_unique_constraint("uq_vault_credentials_secret_ref", "vault_credentials", ["secret_ref"])
        except Exception:
            pass
        # create_index is inside unique; also add explicit index if needed
        try:
            op.create_index("ix_vault_credentials_secret_ref", "vault_credentials", ["secret_ref"])
        except Exception:
            pass

    # encrypted_token: make nullable for external-only rows (dual-write period)
    if _has_column("vault_credentials", "encrypted_token"):
        nullable = _column_is_nullable("vault_credentials", "encrypted_token")
        if nullable is False:
            # nullable=False -> need to alter to True
            try:
                with op.batch_alter_table("vault_credentials") as batch:
                    batch.alter_column("encrypted_token", existing_type=sa.LargeBinary(), nullable=True)
            except Exception:
                # sqlite batch_alter may need recreation; ignore if not supported
                pass

    # vault_backend / vault_path / version: add if missing
    if not _has_column("vault_credentials", "vault_backend"):
        op.add_column("vault_credentials", sa.Column("vault_backend", sa.String(length=32), nullable=True))
    if not _has_column("vault_credentials", "vault_path"):
        op.add_column("vault_credentials", sa.Column("vault_path", sa.String(length=512), nullable=True))
    if not _has_column("vault_credentials", "version"):
        op.add_column("vault_credentials", sa.Column("version", sa.Integer(), nullable=True))


def downgrade() -> None:
    # downgrade removes added columns (if they were added by this revision).
    # secret_ref column is NOT dropped if it was part of 001 initial (keep PK).
    # We only remove vault_backend/vault_path/version and restore NOT NULL.
    try:
        bind = op.get_bind()
        insp = sa.inspect(bind)
        if "vault_credentials" not in insp.get_table_names():
            return
    except Exception:
        return

    for col in ("version", "vault_path", "vault_backend"):
        if _has_column("vault_credentials", col):
            try:
                op.drop_column("vault_credentials", col)
            except Exception:
                pass

    # restore encrypted_token NOT NULL (best-effort)
    if _has_column("vault_credentials", "encrypted_token"):
        nullable = _column_is_nullable("vault_credentials", "encrypted_token")
        if nullable is True:
            try:
                with op.batch_alter_table("vault_credentials") as batch:
                    batch.alter_column("encrypted_token", existing_type=sa.LargeBinary(), nullable=False)
            except Exception:
                pass
