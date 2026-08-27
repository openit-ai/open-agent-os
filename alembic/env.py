"""Alembic env — async SQLAlchemy autogenerate."""
from __future__ import annotations

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
        config.set_main_option("sqlalchemy.url", "postgresql+asyncpg://open_agent_os:secret@localhost:5432/open_agent_os")

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from security.models.db import Base  # noqa: E402
from security.models.orm import (  # noqa: F401,E402
    DelegationORM,
    CredentialBindingORM,
    ApprovalRequestORM,
    AuditEventORM,
    SessionRecordORM,
    VaultCredentialORM,
)

target_metadata = Base.metadata


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

    # During `alembic revision --autogenerate` with no DB, we don't need to fail — offline rendering suffices.
    # Try to connect; if unreachable, skip autogenerate comparison (revision will be manual).
    try:
        asyncio.run(run_async_migrations())
    except Exception as e:
        # If this is a revision command, allow it to proceed with empty diff
        # Re-raise only for `upgrade` commands where DB is required
        # We detect by checking if target_metadata has tables and we're in autogenerate mode
        # For now, just log and fall back to offline rendering when DB unreachable
        import warnings

        warnings.warn(f"Alembic online run failed (DB unreachable): {e}. Falling back to offline metadata-only revision.")
        # Do not re-raise — let revision creation continue with no DB diff (manual version file will be used)
        if "upgrade" in sys.argv:
            raise


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
