"""Admin persistence helper — openagentos (v1.6 §27.3).

Admin Web UI persistence is documented as openagentos PostgreSQL being the
Source of Truth, but runtime uses safe in-memory fallback so all 534 tests
pass without a real DB.

- get_database_url(): checks OAOS_DATABASE_URL then DATABASE_URL
- ensure_admin_tables(): async no-op if no DATABASE_URL, otherwise tries to
  create minimal admin tables (sqlite compat + postgres). No DB call at import
  time; all imports/IO are lazy inside the async function with graceful
  fallback to in-memory. In production (OAOS_ENV=production) fails closed:
  requires DATABASE_URL/OAOS_DATABASE_URL or raises RuntimeError.

Usage:
    from persistence import get_database_url, ensure_admin_tables

    await ensure_admin_tables()  # safe to call on startup; raises in prod if no DB
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def get_database_url() -> str | None:
    """Return DATABASE_URL for admin persistence or None if not configured.

    Priority:
      1. OAOS_DATABASE_URL
      2. DATABASE_URL
    Returns None when neither is set (caller should use in-memory fallback).
    """
    url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if url:
        url = url.strip()
        if not url:
            return None
        return url
    return None


def _normalize_url(url: str) -> str:
    """Normalize DB URL for async SQLAlchemy (postgres/sqlite compat)."""
    u = url.strip()
    # postgres -> asyncpg
    if u.startswith("postgresql://"):
        u = u.replace("postgresql://", "postgresql+asyncpg://", 1)
    # sqlite -> aiosqlite for async
    if u.startswith("sqlite://") and "+aiosqlite" not in u:
        # sqlite:///path or sqlite:///:memory:
        u = u.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return u


# Minimal DDL for admin persistence (openagentos).
# Kept as plain SQL so we don't need ORM imports at runtime.
_ADMIN_DDL = [
    """
    CREATE TABLE IF NOT EXISTS admin_users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL,
        hashed_password TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_infra_services (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        display_name TEXT NOT NULL,
        host TEXT NOT NULL,
        port INTEGER NOT NULL,
        health_path TEXT NOT NULL DEFAULT '/health',
        expected_status INTEGER NOT NULL DEFAULT 200,
        status TEXT NOT NULL DEFAULT 'unknown',
        latency_ms DOUBLE PRECISION,
        last_check TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_user_mappings (
        id TEXT PRIMARY KEY,
        mm_user_id TEXT UNIQUE NOT NULL,
        mm_username TEXT,
        employee_principal TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_by TEXT NOT NULL
    )
    """,
]

# SQLite-compatible DDL (no TIMESTAMPTZ, NOW())
_ADMIN_DDL_SQLITE = [
    """
    CREATE TABLE IF NOT EXISTS admin_users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL,
        hashed_password TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_infra_services (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        display_name TEXT NOT NULL,
        host TEXT NOT NULL,
        port INTEGER NOT NULL,
        health_path TEXT NOT NULL DEFAULT '/health',
        expected_status INTEGER NOT NULL DEFAULT 200,
        status TEXT NOT NULL DEFAULT 'unknown',
        latency_ms REAL,
        last_check TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_user_mappings (
        id TEXT PRIMARY KEY,
        mm_user_id TEXT UNIQUE NOT NULL,
        mm_username TEXT,
        employee_principal TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        created_by TEXT NOT NULL
    )
    """,
]


async def ensure_admin_tables() -> None:
    """Ensure admin persistence tables exist (openagentos), or fallback.

    - No-op with log if no DATABASE_URL is configured (in-memory fallback —
      preserves sqlite compat and keeps tests passing).
    - On any DB error (unreachable, missing driver, auth failure), logs
      warning and returns without raising — caller continues with in-memory
      dicts (auth.py, infra.py, user_mappings.py).
      EXCEPTION: when OAOS_ENV=production, missing DATABASE_URL fails closed
      (raises RuntimeError) and DB errors also propagate instead of falling back.
    - Safe to call multiple times; uses CREATE TABLE IF NOT EXISTS.
    - No DB work happens at import time.
    """
    is_prod = os.environ.get("OAOS_ENV", "").lower() == "production"
    url = get_database_url()
    if not url:
        if is_prod:
            logger.error(
                "Admin persistence: fail-closed — DATABASE_URL/OAOS_DATABASE_URL required when OAOS_ENV=production"
            )
            raise RuntimeError(
                "DATABASE_URL/OAOS_DATABASE_URL required when OAOS_ENV=production (fail-closed)"
            )
        logger.info("Admin persistence: openagentos ready (or in-memory fallback) — no DATABASE_URL, using in-memory")
        return

    normalized = _normalize_url(url)
    is_sqlite = normalized.startswith("sqlite")

    # Lazy imports — keep import-time side-effect free
    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy import text
    except Exception as e:
        if is_prod:
            raise RuntimeError(f"sqlalchemy not available in production: {e}") from e
        logger.warning(f"Admin persistence: in-memory fallback (sqlalchemy not available: {e})")
        return

    ddl_list = _ADMIN_DDL_SQLITE if is_sqlite else _ADMIN_DDL

    engine = None
    try:
        # short timeout; don't block tests
        connect_args = {}
        if is_sqlite:
            # aiosqlite needs no special args
            pass
        engine = create_async_engine(normalized, echo=False, pool_pre_ping=False, connect_args=connect_args)
        async with engine.begin() as conn:
            for ddl in ddl_list:
                await conn.execute(text(ddl))
        logger.info("Admin persistence: openagentos ready")
    except Exception as e:
        if is_prod:
            raise
        # Any failure -> fallback, never raise to caller (non-prod)
        logger.warning(f"Admin persistence: in-memory fallback (DB unavailable: {e})")
        return
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception:
                pass
