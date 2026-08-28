"""Runtime mode selector — Hermes vs LLM Runtime.

GET  /v1/runtime/mode — returns current mode
POST /v1/runtime/mode — sets mode (L5 only), body {mode: "hermes"|"llm"}

Persisted in DB (admin_settings.runtime_mode) > env OAOS_RUNTIME_MODE > in-memory.
DB is authoritative so multi-instance & restart-safe. Falls back to env/in-memory
when DATABASE_URL unavailable (offline/dev mode).
Hermes uses internal LLM via Hermes Agent, so no external provider config needed.
LLM Runtime requires multi-provider config (claude/codex/gemini/opencode-go/openrouter/ollama).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

try:
    from .auth import AdminUser, get_current_admin, require_l5
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/runtime", tags=["runtime"])


class RuntimeMode(str, Enum):
    hermes = "hermes"
    llm = "llm"


# --- DB helpers (mirrors llm_providers.py pattern) ---
_db_engine = None
_db_session_factory = None


def _db_url() -> str | None:
    try:
        try:
            from persistence import get_database_url  # type: ignore
        except ImportError:
            from .persistence import get_database_url  # type: ignore
        url = get_database_url()
        if url and url.strip():
            return url.strip()
    except Exception:
        pass
    url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    return url.strip() if url and url.strip() else None


def _normalize_sync_url(url: str) -> str:
    u = url.strip()
    if u.startswith("postgresql+asyncpg://"):
        u = u.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    elif u.startswith("postgresql://"):
        u = u.replace("postgresql://", "postgresql+psycopg://", 1)
    if "+aiosqlite" in u:
        u = u.replace("+aiosqlite", "")
        u = u.replace("sqlite+://", "sqlite://")
    if u.startswith("sqlite+"):
        u = u.replace("sqlite+", "sqlite", 1)
    return u


def _get_engine():
    global _db_engine
    if _db_engine is not None:
        return _db_engine
    url = _db_url()
    if not url:
        return None
    try:
        from sqlalchemy import create_engine
        _db_engine = create_engine(_normalize_sync_url(url), pool_pre_ping=True) if not _normalize_sync_url(url).startswith("sqlite") else create_engine(_normalize_sync_url(url))
        return _db_engine
    except Exception as e:
        logger.debug(f"runtime_mode DB engine failed: {e}")
        return None


def _db_get_mode() -> str | None:
    try:
        engine = _get_engine()
        if engine is None:
            return None
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(text("SELECT value FROM admin_settings WHERE key='runtime_mode'")).fetchone()
            if row and row[0] in ("hermes", "llm"):
                return row[0]
    except Exception as e:
        logger.debug(f"runtime_mode DB read failed: {e}")
    return None


def _db_set_mode(value: str) -> bool:
    try:
        engine = _get_engine()
        if engine is None:
            return False
        from sqlalchemy import text
        now = datetime.now(timezone.utc).isoformat()
        # Ensure table exists (idempotent, for offline sqlite)
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE IF NOT EXISTS admin_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT, updated_by TEXT, extra TEXT)"))
        except Exception:
            pass
        with engine.begin() as conn:
            # Try Postgres ON CONFLICT first
            try:
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at) VALUES ('runtime_mode', :v, :now) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now"), {"v": value, "now": now})
                return True
            except Exception:
                pass
            # SQLite fallback
            try:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at) VALUES ('runtime_mode', :v, :now)"), {"v": value, "now": now})
                return True
            except Exception as e2:
                logger.debug(f"runtime_mode DB write fallback failed: {e2}")
                return False
    except Exception as e:
        logger.debug(f"runtime_mode DB write failed: {e}")
        return False


# In-memory state, initialized from env/DB if present
def _init_mode() -> RuntimeMode:
    # DB has priority if reachable
    try:
        db_v = _db_get_mode()
        if db_v in ("hermes", "llm"):
            return RuntimeMode(db_v)
    except Exception:
        pass
    env_v = os.environ.get("OAOS_RUNTIME_MODE", "").lower()
    if env_v in ("hermes", "llm"):
        return RuntimeMode(env_v)
    return RuntimeMode.hermes


_current_mode: RuntimeMode = _init_mode()


def get_mode() -> RuntimeMode:
    # DB is authoritative — check on each call (cheap for sqlite/postgres, fallback to memory if DB unavailable)
    try:
        db_v = _db_get_mode()
        if db_v in ("hermes", "llm"):
            # sync in-memory + env if DB differs
            global _current_mode
            if _current_mode.value != db_v:
                _current_mode = RuntimeMode(db_v)
                os.environ["OAOS_RUNTIME_MODE"] = db_v
            return _current_mode
    except Exception:
        pass
    return _current_mode


def set_mode(m: RuntimeMode) -> RuntimeMode:
    global _current_mode
    _current_mode = m
    os.environ["OAOS_RUNTIME_MODE"] = m.value
    # Persist to DB (best-effort, fail-soft)
    _db_set_mode(m.value)
    return _current_mode


class ModeRequest(BaseModel):
    mode: RuntimeMode


@router.get("/mode")
def get_runtime_mode(admin: AdminUser = Depends(get_current_admin)):
    mode = get_mode()
    return {"mode": mode.value, "available_modes": [e.value for e in RuntimeMode]}


@router.post("/mode")
def post_runtime_mode(body: ModeRequest, admin: AdminUser = Depends(require_l5)):
    new_mode = set_mode(body.mode)
    return {"mode": new_mode.value, "available_modes": [e.value for e in RuntimeMode], "updated_by": admin.email}
