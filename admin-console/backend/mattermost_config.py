"""Mattermost config API — MM server + bot identity for the personal agent (admin-console/backend/mattermost_config.py).

GET  /v1/mattermost/config — read effective config (auth; token only as set-flag)
PUT  /v1/mattermost/config — L5 update (admin_settings.mm_config JSON; token write-only)
POST /v1/mattermost/test   — L5 live probe (GET /api/v4/users/me with stored or one-shot token)

Precedence: DB mm_config JSON > MATTERMOST_* env > defaults.
NOTE: the live bridge/Control Plane reads MATTERMOST_* env at startup. DB values
are the console source of truth; applying to runtime requires env update +
service restart (response flags applied=false). Secrets never returned/logged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

try:
    from .auth import AdminUser, get_current_admin, require_l5
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/mattermost", tags=["mattermost"])

MM_KEY = "mm_config"
DEFAULT_URL = "http://127.0.0.1:8065"
DEFAULT_BOT_USERNAME = "agent"

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
        logger.debug(f"mm DB engine failed: {e}")
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
            row = conn.execute(text("SELECT value FROM admin_settings WHERE key='mm_config'")).fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logger.debug(f"mm DB read failed: {e}")
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
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES ('mm_config', :v, :now, :by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception:
                pass
            try:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES ('mm_config', :v, :now, :by)"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception as e2:
                logger.debug(f"mm DB write fallback failed: {e2}")
                return False
    except Exception as e:
        logger.debug(f"mm DB write failed: {e}")
        return False


def _env_config() -> dict:
    return {
        "mattermost_url": (os.environ.get("MATTERMOST_URL") or DEFAULT_URL).strip(),
        "bot_token_set": bool((os.environ.get("MATTERMOST_TOKEN")
                               or os.environ.get("MATTERMOST_BOT_TOKEN") or "").strip()),
        "bot_username": (os.environ.get("MATTERMOST_BOT_USERNAME") or DEFAULT_BOT_USERNAME).strip(),
        "default_display_name": (os.environ.get("MATTERMOST_DEFAULT_DISPLAY_NAME") or "").strip(),
    }


def _load_config() -> tuple[dict, str]:
    global _inmem
    raw = _db_get_raw()
    if raw:
        try:
            data = json.loads(raw)
            env = _env_config()
            cfg = {
                "mattermost_url": str(data.get("mattermost_url") or env["mattermost_url"]),
                "bot_token_set": bool(data.get("bot_token_set", False)) or env["bot_token_set"],
                "bot_username": str(data.get("bot_username") or env["bot_username"]),
                "default_display_name": str(data.get("default_display_name") or env["default_display_name"]),
            }
            _inmem = cfg
            return cfg, "db"
        except Exception as e:
            logger.debug(f"mm parse DB failed: {e}")
    if _inmem is not None:
        return dict(_inmem), "in-memory"
    return _env_config(), "env"


class MmUpdateRequest(BaseModel):
    mattermost_url: Optional[str] = Field(default=None, max_length=256)
    bot_token: Optional[str] = Field(default=None, max_length=256)
    bot_username: Optional[str] = Field(default=None, max_length=64)
    default_display_name: Optional[str] = Field(default=None, max_length=128)

    @field_validator("mattermost_url")
    @classmethod
    def check_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().rstrip("/")
        if not v:
            raise ValueError("mattermost_url must not be empty")
        low = v.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            raise ValueError("mattermost_url must start with http:// or https://")
        return v

    @field_validator("bot_token")
    @classmethod
    def check_token(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("bot_token must not be empty")
        return v

    @field_validator("bot_username")
    @classmethod
    def check_username(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().lower().lstrip("@")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", v or ""):
            raise ValueError("bot_username must match ^[a-z0-9][a-z0-9._-]{0,63}$")
        return v


@router.get("/config")
def mm_get_config(admin: AdminUser = Depends(get_current_admin)) -> dict:
    cfg, source = _load_config()
    return {**cfg, "source": source, "applied": source == "env",
            "note": "DB values require MATTERMOST_* env update + restart to apply" if source != "env" else "live env values in effect"}


@router.put("/config")
def mm_put_config(req: MmUpdateRequest, admin: AdminUser = Depends(require_l5)) -> dict:
    global _inmem
    cfg, _ = _load_config()
    if req.mattermost_url is not None:
        cfg["mattermost_url"] = req.mattermost_url
    if req.bot_username is not None:
        cfg["bot_username"] = req.bot_username
    if req.default_display_name is not None:
        cfg["default_display_name"] = req.default_display_name.strip()
    if req.bot_token is not None:
        cfg["bot_token_set"] = True
    raw = json.dumps(cfg)
    ok = _db_set_raw(raw, updated_by=getattr(admin, "email", None))
    if ok:
        _inmem = dict(cfg)
        return {**cfg, "source": "db", "applied": False,
                "note": "saved; update MATTERMOST_* env on the host and restart services to apply"}
    if (os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod")):
        raise HTTPException(status_code=503, detail="Mattermost config DB unavailable in production (fail-closed)")
    _inmem = dict(cfg)
    return {**cfg, "source": "in-memory", "applied": False,
            "note": "saved in-memory only (dev); update MATTERMOST_* env and restart to apply"}


@router.post("/test")
def mm_test(body: dict | None = None, admin: AdminUser = Depends(require_l5)) -> dict:
    """Probe Mattermost with the stored config (optional one-shot token override, never stored)."""
    cfg, source = _load_config()
    override = ""
    if isinstance(body, dict):
        override = str(body.get("bot_token") or "").strip()
    token = override or os.environ.get("MATTERMOST_TOKEN") or os.environ.get("MATTERMOST_BOT_TOKEN") or ""
    if not token:
        return {"ok": False, "error": "no bot token configured (save one first or pass bot_token for a one-shot probe)", "source": source}
    target = cfg["mattermost_url"]
    t0 = time.monotonic()
    try:
        import httpx
        r = httpx.get(target + "/api/v4/users/me", headers={"Authorization": f"Bearer {token}"}, timeout=5.0)
        ms = round((time.monotonic() - t0) * 1000, 1)
        if r.status_code == 200:
            data = r.json()
            return {"ok": True, "target": target, "status_code": 200,
                    "bot_user_id": data.get("id"), "bot_username": data.get("username"),
                    "latency_ms": ms, "source": source}
        return {"ok": False, "target": target, "status_code": r.status_code,
                "error": r.text[:200], "latency_ms": ms, "source": source}
    except Exception as e:
        return {"ok": False, "target": target,
                "error": f"{type(e).__name__}: {str(e)[:160]}", "source": source}
