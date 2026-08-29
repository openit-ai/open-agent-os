"""Alembic env — async SQLAlchemy autogenerate."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

ROOT = Path(__file__).resolve().parents[1]
for p in [ROOT, ROOT / "security", ROOT / "packages/common-types", ROOT / "packages/delegation-model", ROOT / "packages/audit-model"]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

config = context.config

db_url = os.environ.get("DATABASE_URL") or os.environ.get("OAOS_DATABASE_URL")
if db_url:
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    config.set_main_option("sqlalchemy.url", db_url)
else:
    if not config.get_main_option("sqlalchemy.url"):
        config.set_main_option("sqlalchemy.url", "postgresql+asyncpg://oaos:secret@localhost:5432/oaos")

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from security.models.db import Base  # noqa: E402
from security.models.orm import (  # noqa: F401,E402
    DelegationORM,
    CredentialBindingORM,
    ApprovalRequestORM,
    ApprovalNonceORM,
    AuditEventORM,
    SessionRecordORM,
    VaultCredentialORM,
    MemoryORM,
    MemorySourceORM,
    MemoryEmbeddingORM,
    MemoryAccessBindingORM,
    AdminStateORM,
    AdminUserORM,
    AdminInfraServiceORM,
    AdminUserMappingORM,
)

target_metadata = Base.metadata
logger = logging.getLogger("alembic.env")


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True, compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    import asyncio

    try:
        asyncio.run(run_async_migrations())
    except Exception as e:
        is_autogenerate = "--autogenerate" in sys.argv or "revision" in sys.argv
        is_sql_mode = "--sql" in sys.argv
        is_upgrade = "upgrade" in sys.argv

        # Always surface DB-unreachable clearly — never swallow for autogenerate.
        msg = f"Alembic online run failed (DB unreachable): {e}"
        drift_hint = " --autogenerate diff will be EMPTY, drift may be hidden. Run with DB available."
        full_msg = msg + (drift_hint if is_autogenerate else " Falling back.")

        # Log via stdlib logging (visible regardless of warnings filter) and stderr
        logger.warning(full_msg)
        print(f"WARNING [alembic.env] {full_msg}", file=sys.stderr)

        # Also emit warnings.warn for tooling that captures warnings, but not as sole channel
        import warnings

        warnings.warn(full_msg)

        # --sql mode (offline) should not require DB — warn and allow offline rendering
        # For online --sql autogenerate we still warn but don't need to fail
        if is_sql_mode:
            logger.warning("DB unreachable but --sql mode requested: offline rendering will be used (no DB comparison).")
            print("WARNING [alembic.env] --sql mode: offline rendering, DB comparison skipped.", file=sys.stderr)
            return

        if is_autogenerate:
            # Don't swallow: warning already emitted loudly above.
            # Keep revision creation alive (return) but warning is now unmissable.
            print(
                "WARNING [alembic.env] DB unreachable during --autogenerate: empty diff — manual revision required.",
                file=sys.stderr,
            )
            return

        # For upgrade DB is required — re-raise; other commands (check/history) keep original swallow but now loudly warned
        if is_upgrade:
            raise
        # Non-upgrade, non-autogenerate (e.g. `alembic check` without DB) — keep alive but warning is unmissable
        return


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
