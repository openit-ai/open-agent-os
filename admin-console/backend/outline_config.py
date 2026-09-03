"""Outline config API — enterprise wiki connector for Knowledge Index (admin-console/backend/outline_config.py).

GET  /v1/outline/config — read effective config (auth; key only as set-flag)
PUT  /v1/outline/config — L5 update (admin_settings.outline_config JSON; key write-only)
POST /v1/outline/test   — L5 live probe (GET {url}/api/collections with key)

Precedence: DB outline_config JSON > OUTLINE_* env > defaults.
NOTE: the sync worker reads OUTLINE_* env at startup. DB values are the console
source of truth; applying requires env update + restart (applied=false).
Secrets never returned/logged.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

try:
    from .auth import AdminUser, get_current_admin, require_l5
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/outline", tags=["outline"])

OL_KEY = "outline_config"
DEFAULT_URL = "http://127.0.0.1:3000"

_db_engine = None
_inmem: dict | None = None


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
        logger.debug(f"outline DB engine failed: {e}")
        return None


def _ensure_table(engine) -> None:
    try:
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS admin_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT, updated_by TEXT, extra TEXT)"))
    except Exception:
        pass


def _db_get_raw() -> str | None:
    try:
        engine = _get_engine()
        if engine is None:
            return None
        _ensure_table(engine)
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(text("SELECT value FROM admin_settings WHERE key='outline_config'")).fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logger.debug(f"outline DB read failed: {e}")
    return None


def _db_set_raw(value_json: str, updated_by: str | None = None) -> bool:
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
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES ('outline_config', :v, :now, :by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception:
                pass
            try:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES ('outline_config', :v, :now, :by)"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception as e2:
                logger.debug(f"outline DB write fallback failed: {e2}")
                return False
    except Exception as e:
        logger.debug(f"outline DB write failed: {e}")
        return False


def _env_config() -> dict:
    return {
        "outline_url": (os.environ.get("OUTLINE_API_URL") or os.environ.get("OUTLINE_URL") or DEFAULT_URL).strip(),
        "api_key_set": bool((os.environ.get("OUTLINE_API_KEY") or "").strip()),
    }


def _load_config() -> tuple[dict, str]:
    global _inmem
    raw = _db_get_raw()
    if raw:
        try:
            data = json.loads(raw)
            env = _env_config()
            cfg = {
                "outline_url": str(data.get("outline_url") or env["outline_url"]),
                "api_key_set": bool(data.get("api_key_set", False)) or env["api_key_set"],
            }
            _inmem = cfg
            return cfg, "db"
        except Exception as e:
            logger.debug(f"outline parse DB failed: {e}")
    if _inmem is not None:
        return dict(_inmem), "in-memory"
    return _env_config(), "env"


class OutlineUpdateRequest(BaseModel):
    outline_url: Optional[str] = Field(default=None, max_length=256)
    api_key: Optional[str] = Field(default=None, max_length=256)

    @field_validator("outline_url")
    @classmethod
    def check_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().rstrip("/")
        if not v:
            raise ValueError("outline_url must not be empty")
        low = v.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            raise ValueError("outline_url must start with http:// or https://")
        return v

    @field_validator("api_key")
    @classmethod
    def check_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("api_key must not be empty")
        return v


@router.get("/config")
def ol_get_config(admin: AdminUser = Depends(get_current_admin)) -> dict:
    cfg, source = _load_config()
    return {**cfg, "source": source, "applied": source == "env",
            "note": "DB values require OUTLINE_* env update + restart to apply" if source != "env" else "live env values in effect"}


@router.put("/config")
def ol_put_config(req: OutlineUpdateRequest, admin: AdminUser = Depends(require_l5)) -> dict:
    global _inmem
    cfg, _ = _load_config()
    if req.outline_url is not None:
        cfg["outline_url"] = req.outline_url
    if req.api_key is not None:
        cfg["api_key_set"] = True
    raw = json.dumps(cfg)
    ok = _db_set_raw(raw, updated_by=getattr(admin, "email", None))
    if ok:
        _inmem = dict(cfg)
        return {**cfg, "source": "db", "applied": False,
                "note": "saved; update OUTLINE_* env on the host and restart services to apply"}
    if (os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod")):
        raise HTTPException(status_code=503, detail="Outline config DB unavailable in production (fail-closed)")
    _inmem = dict(cfg)
    return {**cfg, "source": "in-memory", "applied": False,
            "note": "saved in-memory only (dev); update OUTLINE_* env and restart to apply"}


@router.post("/test")
def ol_test(body: dict | None = None, admin: AdminUser = Depends(require_l5)) -> dict:
    """Probe Outline with the stored config (optional one-shot key override, never stored)."""
    cfg, source = _load_config()
    override = ""
    if isinstance(body, dict):
        override = str(body.get("api_key") or "").strip()
    key = override or os.environ.get("OUTLINE_API_KEY") or ""
    if not key:
        return {"ok": False, "error": "no API key configured (save one first or pass api_key for a one-shot probe)", "source": source}
    target = cfg["outline_url"]
    t0 = time.monotonic()
    try:
        import httpx
        r = httpx.post(target + "/api/collections.list", json={"token": key}, timeout=8.0)
        ms = round((time.monotonic() - t0) * 1000, 1)
        if r.status_code == 200:
            try:
                n = len(r.json().get("data", []))
            except Exception:
                n = -1
            return {"ok": True, "target": target, "status_code": 200,
                    "collection_count": n, "latency_ms": ms, "source": source}
        return {"ok": False, "target": target, "status_code": r.status_code,
                "error": r.text[:200], "latency_ms": ms, "source": source}
    except Exception as e:
        return {"ok": False, "target": target,
                "error": f"{type(e).__name__}: {str(e)[:160]}", "source": source}
