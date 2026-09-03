"""ACP config API — Agent runtime (Hermes/ACP) connection settings (admin-console/backend/acp_config.py).

GET  /v1/acp/config — read effective ACP config (any authenticated admin; API key only as set-flag)
PUT  /v1/acp/config — L5 update (persisted to admin_settings.acp_config JSON)
POST /v1/acp/test   — L5 probe base_url (/health, then /v1/models)

Precedence: DB acp_config JSON > OAOS_CP_* env > defaults.
NOTE: the live Control Plane reads OAOS_CP_* env at startup. DB values take effect
for the Admin Console and are mirrored to env for consumers, but the running
Control Plane requires env update + restart to apply (response flags applied=false).
Secrets are never returned and never logged.
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
router = APIRouter(prefix="/v1/acp", tags=["acp"])

ACP_KEY = "acp_config"
DEFAULT_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_MODEL = "qwen2.5"

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
        logger.debug(f"acp DB engine failed: {e}")
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
            row = conn.execute(text("SELECT value FROM admin_settings WHERE key='acp_config'")).fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logger.debug(f"acp DB read failed: {e}")
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
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES ('acp_config', :v, :now, :by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception:
                pass
            try:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES ('acp_config', :v, :now, :by)"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception as e2:
                logger.debug(f"acp DB write fallback failed: {e2}")
                return False
    except Exception as e:
        logger.debug(f"acp DB write failed: {e}")
        return False


def _env_config() -> dict:
    acp_enabled_raw = (os.environ.get("OAOS_CP_HERMES_ACP_ENABLED") or "").strip().lower()
    return {
        "hermes_base_url": (os.environ.get("OAOS_CP_HERMES_BASE_URL")
                            or os.environ.get("HERMES_BASE_URL") or DEFAULT_BASE_URL).strip(),
        "hermes_model": (os.environ.get("OAOS_CP_HERMES_MODEL") or DEFAULT_MODEL).strip(),
        "acp_enabled": acp_enabled_raw in ("1", "true", "yes"),
        "api_key_set": bool((os.environ.get("OAOS_CP_HERMES_API_KEY")
                             or os.environ.get("HERMES_API_KEY") or "").strip()),
    }


def _load_config() -> tuple[dict, str]:
    """Return (config, source). Never includes secret values."""
    global _inmem
    raw = _db_get_raw()
    if raw:
        try:
            data = json.loads(raw)
            cfg = {
                "hermes_base_url": str(data.get("hermes_base_url") or _env_config()["hermes_base_url"]),
                "hermes_model": str(data.get("hermes_model") or _env_config()["hermes_model"]),
                "acp_enabled": bool(data.get("acp_enabled", False)),
                "api_key_set": bool(data.get("api_key_set", False)) or _env_config()["api_key_set"],
            }
            _inmem = cfg
            return cfg, "db"
        except Exception as e:
            logger.debug(f"acp parse DB failed: {e}")
    if _inmem is not None:
        return dict(_inmem), "in-memory"
    return _env_config(), "env"


def _validate_base_url(v: str) -> str:
    v = (v or "").strip()
    if not v:
        raise ValueError("hermes_base_url required")
    if len(v) > 256:
        raise ValueError("hermes_base_url too long (max 256)")
    low = v.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        raise ValueError("hermes_base_url must start with http:// or https://")
    return v.rstrip("/")


class AcpUpdateRequest(BaseModel):
    hermes_base_url: Optional[str] = Field(default=None, max_length=256)
    hermes_model: Optional[str] = Field(default=None, max_length=128)
    acp_enabled: Optional[bool] = None

    @field_validator("hermes_base_url")
    @classmethod
    def check_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_base_url(v)

    @field_validator("hermes_model")
    @classmethod
    def check_model(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("hermes_model must not be empty")
        low = v.lower()
        if "gpt-5.6-luna" in low or "gpt-5.6-sol" in low or "gpt-5.6" in low:
            raise ValueError("model 'gpt-5.6-luna/sol' is not allowed (blocked)")
        return v


@router.get("/config")
def acp_get_config(admin: AdminUser = Depends(get_current_admin)) -> dict:
    cfg, source = _load_config()
    return {**cfg, "source": source, "applied": source == "env",
            "note": "DB values require Control Plane env update + restart to apply" if source != "env" else "live env values in effect"}


@router.put("/config")
def acp_put_config(req: AcpUpdateRequest, admin: AdminUser = Depends(require_l5)) -> dict:
    global _inmem
    cfg, _ = _load_config()
    if req.hermes_base_url is not None:
        cfg["hermes_base_url"] = req.hermes_base_url
    if req.hermes_model is not None:
        cfg["hermes_model"] = req.hermes_model
    if req.acp_enabled is not None:
        cfg["acp_enabled"] = req.acp_enabled
    raw = json.dumps(cfg)
    ok = _db_set_raw(raw, updated_by=getattr(admin, "email", None))
    if ok:
        _inmem = dict(cfg)
        os.environ["OAOS_ACP_CONFIG_JSON"] = raw
        return {**cfg, "source": "db", "applied": False,
                "note": "saved; update OAOS_CP_* env on the Control Plane host and restart to apply"}
    if (os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod")):
        raise HTTPException(status_code=503, detail="ACP config DB unavailable in production (fail-closed)")
    _inmem = dict(cfg)
    return {**cfg, "source": "in-memory", "applied": False,
            "note": "saved in-memory only (dev); update OAOS_CP_* env and restart to apply"}


@router.post("/test")
def acp_test(body: dict | None = None, admin: AdminUser = Depends(require_l5)) -> dict:
    """Probe a base_url (stored config by default; optional one-shot override, never stored)."""
    cfg, source = _load_config()
    override = ""
    if isinstance(body, dict):
        override = str(body.get("hermes_base_url") or "").strip()
    target = _validate_base_url(override) if override else cfg["hermes_base_url"]
    t0 = time.monotonic()
    try:
        import httpx
        r = httpx.get(target + "/health", timeout=5.0)
        if r.status_code < 500:
            return {"ok": r.status_code < 400, "target": target, "path": "/health",
                    "status_code": r.status_code,
                    "latency_ms": round((time.monotonic() - t0) * 1000, 1), "source": source}
        r2 = httpx.get(target + "/v1/models", timeout=5.0)
        return {"ok": r2.status_code < 400, "target": target, "path": "/v1/models",
                "status_code": r2.status_code,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1), "source": source}
    except Exception as e:
        return {"ok": False, "target": target,
                "error": f"{type(e).__name__}: {str(e)[:160]}", "source": source}
