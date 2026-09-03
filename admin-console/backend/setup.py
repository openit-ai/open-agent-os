"""Setup wizard API — first-run status, connectivity checks, completion (admin-console/backend/setup.py).

GET  /v1/setup/status   — public. {first_run, setup_completed, has_admin}
POST /v1/setup/checks   — L5. {db_url?, redis_url?, hermes_url?} connectivity probe (no secrets echoed).
POST /v1/setup/complete — L5. Mark setup_completed=true.

Persists in DB admin_settings.setup_completed > in-memory.
Fail-closed in production when DB unavailable (explicit 503, no silent mock).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

try:
    from .auth import AdminUser, get_current_admin, require_l5
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/setup", tags=["setup"])

SETUP_KEY = "setup_completed"

_db_engine = None
_inmem_completed: bool | None = None


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
        sync_url = _normalize_sync_url(url)
        kwargs: dict = {"pool_pre_ping": True}
        if sync_url.startswith("sqlite"):
            kwargs = {}
            if ":memory:" in sync_url:
                kwargs["connect_args"] = {"check_same_thread": False}
        _db_engine = create_engine(sync_url, **kwargs)
        return _db_engine
    except Exception as e:
        logger.debug(f"setup DB engine failed: {e}")
        return None


def _ensure_table(engine) -> None:
    try:
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS admin_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT, updated_by TEXT, extra TEXT)"))
    except Exception:
        pass


def _is_production() -> bool:
    return (os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod"))


def _db_get_completed() -> bool | None:
    try:
        engine = _get_engine()
        if engine is None:
            return None
        _ensure_table(engine)
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(text("SELECT value FROM admin_settings WHERE key='setup_completed'")).fetchone()
            if row and row[0]:
                return str(row[0]).strip().lower() in ("1", "true", "yes")
            return False
    except Exception as e:
        logger.debug(f"setup DB read failed: {e}")
        return None


def _db_set_completed(updated_by: str | None = None) -> bool:
    try:
        engine = _get_engine()
        if engine is None:
            return False
        _ensure_table(engine)
        from sqlalchemy import text
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with engine.begin() as conn:
            try:
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES ('setup_completed', 'true', :now, :by) ON CONFLICT (key) DO UPDATE SET value='true', updated_at=:now, updated_by=:by"),
                             {"now": now, "by": updated_by})
                return True
            except Exception:
                pass
            try:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES ('setup_completed', 'true', :now, :by)"),
                             {"now": now, "by": updated_by})
                return True
            except Exception as e2:
                logger.debug(f"setup DB write fallback failed: {e2}")
                return False
    except Exception as e:
        logger.debug(f"setup DB write failed: {e}")
        return False


def _has_admin() -> bool:
    try:
        try:
            from auth import list_users  # type: ignore
        except ImportError:
            from .auth import list_users  # type: ignore
        return len(list_users()) > 0
    except Exception:
        return False


class ChecksRequest(BaseModel):
    db_url: Optional[str] = Field(default=None, max_length=512)
    redis_url: Optional[str] = Field(default=None, max_length=512)
    hermes_url: Optional[str] = Field(default=None, max_length=512)


def _check_db(url: str | None) -> dict:
    target = (url or "").strip() or _db_url()
    if not target:
        return {"ok": False, "error": "no DATABASE_URL configured"}
    t0 = time.monotonic()
    try:
        from sqlalchemy import create_engine, text
        sync_url = _normalize_sync_url(target)
        kwargs: dict = {"connect_args": {"connect_timeout": 5}}
        if sync_url.startswith("sqlite"):
            kwargs = {}
        eng = create_engine(sync_url, pool_pre_ping=False, **kwargs)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return {"ok": True, "latency_ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _check_redis(url: str | None) -> dict:
    target = (url or "").strip() or os.environ.get("OAOS_REDIS_URL") or os.environ.get("REDIS_URL") or "redis://127.0.0.1:6379/0"
    t0 = time.monotonic()
    try:
        import redis as redis_lib
        client = redis_lib.Redis.from_url(target, socket_connect_timeout=3, socket_timeout=3)
        client.ping()
        return {"ok": True, "latency_ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _check_hermes(url: str | None) -> dict:
    target = ((url or "").strip()
              or os.environ.get("OAOS_CP_HERMES_BASE_URL")
              or os.environ.get("HERMES_BASE_URL")
              or "http://127.0.0.1:8001").rstrip("/")
    t0 = time.monotonic()
    try:
        import httpx
        for path in ("/health", "/v1/models"):
            try:
                r = httpx.get(target + path, timeout=5.0)
                if r.status_code < 500:
                    return {"ok": r.status_code < 400, "status_code": r.status_code,
                            "latency_ms": round((time.monotonic() - t0) * 1000, 1)}
            except Exception:
                continue
        return {"ok": False, "error": "unreachable (/health and /v1/models failed)"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


@router.get("/status")
def setup_status() -> dict:
    """Public first-run status — no secrets, no auth (needed before login)."""
    global _inmem_completed
    completed = _db_get_completed()
    if completed is None:
        completed = bool(_inmem_completed) if _inmem_completed is not None else False
    return {
        "first_run": not completed,
        "setup_completed": completed,
        "has_admin": _has_admin(),
    }


@router.post("/checks")
def setup_checks(req: ChecksRequest, admin: AdminUser = Depends(require_l5)) -> dict:
    """L5 connectivity checks. Supplied URLs are used for probing only (never stored)."""
    return {
        "db": _check_db(req.db_url),
        "redis": _check_redis(req.redis_url),
        "hermes": _check_hermes(req.hermes_url),
    }


@router.post("/complete")
def setup_complete(admin: AdminUser = Depends(require_l5)) -> dict:
    """L5 mark setup completed. Fail-closed in production when DB unavailable."""
    global _inmem_completed
    ok = _db_set_completed(updated_by=getattr(admin, "email", None))
    if ok:
        _inmem_completed = True
        return {"setup_completed": True, "persisted": "db"}
    if _is_production():
        raise HTTPException(status_code=503, detail="setup state DB unavailable in production (fail-closed)")
    _inmem_completed = True
    return {"setup_completed": True, "persisted": "in-memory"}
