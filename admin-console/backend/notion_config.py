"""Notion config API — Knowledge Index Notion connector (admin-console/backend/notion_config.py).

GET  /v1/notion/config — read effective config (auth; key only as set-flag)
PUT  /v1/notion/config — L5 update (admin_settings.notion_config JSON; key write-only)
POST /v1/notion/test   — L5 live probe (GET {url}/v1/users with key)

Precedence: DB notion_config JSON > NOTION_* env > defaults.
NOTE: the sync worker reads NOTION_* env at startup. DB values are the console
source of truth; applying requires env update + restart (applied=false).
Secrets never returned/logged. Independent of MM/Outline modules (own helpers,
own admin_settings key).
"""
from __future__ import annotations

import json
import logging
import os
import time
import base64
import hashlib
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

try:
    from .auth import AdminUser, get_current_admin, require_l5
    from .config_state import config_response
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore
    from config_state import config_response  # type: ignore

logger = logging.getLogger(__name__)

try:
    from sqlalchemy.exc import SQLAlchemyError
except (ImportError, ModuleNotFoundError):  # sqlalchemy is lazy/optional; best-effort fallback
    SQLAlchemyError = Exception  # type: ignore
router = APIRouter(prefix="/v1/notion", tags=["notion"])

NOTION_KEY = "notion_config"
DEFAULT_URL = "https://api.notion.com"
NOTION_VERSION = "2022-06-28"
_DEV_VAULT_KEY = "dev-notion-config-vault-key-please-change"
_SECRET_REF = "vault://admin_settings/notion_config/api_key"

_db_engine = None
_inmem: dict | None = None
_fernet_cache: dict[str, object] = {}


def _db_url() -> str | None:
    try:
        try:
            from persistence import get_database_url  # type: ignore
        except ImportError:
            from .persistence import get_database_url  # type: ignore
        url = get_database_url()
        if url and url.strip():
            return url.strip()
    except (ImportError, ModuleNotFoundError, AttributeError):
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
    except (ImportError, ModuleNotFoundError, SQLAlchemyError) as e:
        logger.debug(f"notion DB engine failed: {e}")
        return None


def _ensure_table(engine) -> None:
    try:
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS admin_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT, updated_by TEXT, extra TEXT)"))
    except (ImportError, ModuleNotFoundError, SQLAlchemyError):
        pass


def _db_get_raw() -> str | None:
    try:
        engine = _get_engine()
        if engine is None:
            return None
        _ensure_table(engine)
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(text("SELECT value FROM admin_settings WHERE key='notion_config'")).fetchone()
            if row and row[0]:
                return row[0]
    except (ImportError, ModuleNotFoundError, SQLAlchemyError) as e:
        logger.debug(f"notion DB read failed: {e}")
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
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES ('notion_config', :v, :now, :by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except (ImportError, ModuleNotFoundError, SQLAlchemyError):
                pass
            try:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES ('notion_config', :v, :now, :by)"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except (ImportError, ModuleNotFoundError, SQLAlchemyError) as e2:
                logger.debug(f"notion DB write fallback failed: {e2}")
                return False
    except (ImportError, ModuleNotFoundError, SQLAlchemyError) as e:
        logger.debug(f"notion DB write failed: {e}")
        return False


def _env_config() -> dict:
    url = (os.environ.get("NOTION_API_URL") or os.environ.get("OAOS_NOTION_URL")
           or os.environ.get("OAOS_NOTION_API_URL") or DEFAULT_URL).strip()
    return {
        "notion_api_url": url,
        "api_key_set": bool((os.environ.get("NOTION_API_KEY") or os.environ.get("NOTION_TOKEN")
                             or os.environ.get("OAOS_NOTION_TOKEN") or os.environ.get("NOTION_API_TOKEN") or "").strip()),
    }


def _get_raw_vault_key() -> bytes:
    raw = (os.environ.get("OAOS_VAULT_KEY") or os.environ.get("VAULT_ENCRYPTION_KEY") or "").strip()
    if not raw:
        if os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod"):
            raise RuntimeError("OAOS_VAULT_KEY/VAULT_ENCRYPTION_KEY must be set in production")
        raw = _DEV_VAULT_KEY
    return raw.encode("utf-8")


def _get_fernet():
    raw = _get_raw_vault_key()
    cache_key = raw.hex()
    if cache_key in _fernet_cache:
        return _fernet_cache[cache_key]
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError("cryptography is required for Notion secret storage") from exc
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    fernet = Fernet(key)
    _fernet_cache[cache_key] = fernet
    return fernet


def _encrypt_api_key(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def _decrypt_api_key(encrypted: str | None) -> str | None:
    if not encrypted:
        return None
    try:
        return _get_fernet().decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - do not leak ciphertext or token details
        logger.warning("notion secret reference resolution failed: %s", type(exc).__name__)
        return None


def _read_saved_payload() -> dict:
    raw = _db_get_raw()
    if raw:
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}
    return dict(_inmem or {})


def _public_config(data: dict, env: dict) -> dict:
    encrypted = str(data.get("encrypted_api_key") or "").strip()
    secret_ref = str(data.get("secret_ref") or "").strip()
    return {
        "notion_api_url": str(data.get("notion_api_url") or env["notion_api_url"]),
        "api_key_set": bool(data.get("api_key_set", False)) or bool(encrypted and secret_ref) or env["api_key_set"],
    }


def _load_config() -> tuple[dict, str]:
    global _inmem
    raw = _db_get_raw()
    if raw:
        try:
            data = json.loads(raw)
            env = _env_config()
            cfg = _public_config(data, env)
            _inmem = cfg
            return cfg, "db"
        except (ValueError, AttributeError, TypeError) as e:
            logger.debug(f"notion parse DB failed: {e}")
    if _inmem is not None:
        return _public_config(dict(_inmem), _env_config()), "in-memory"
    return _env_config(), "env"


def _env_api_key() -> str:
    return (os.environ.get("NOTION_API_KEY") or os.environ.get("NOTION_TOKEN")
            or os.environ.get("OAOS_NOTION_TOKEN") or os.environ.get("NOTION_API_TOKEN") or "").strip()


def resolve_api_key(override: str | None = None) -> str:
    """Resolve a Notion API key without logging or returning secret metadata."""
    if override and override.strip():
        return override.strip()
    env_key = _env_api_key()
    if env_key:
        return env_key
    payload = _read_saved_payload()
    if payload.get("secret_ref") != _SECRET_REF:
        return ""
    return _decrypt_api_key(str(payload.get("encrypted_api_key") or "")) or ""


class NotionUpdateRequest(BaseModel):
    notion_api_url: Optional[str] = Field(default=None, max_length=256)
    api_key: Optional[str] = Field(default=None, max_length=512)

    @field_validator("notion_api_url")
    @classmethod
    def check_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().rstrip("/")
        if not v:
            raise ValueError("notion_api_url must not be empty")
        low = v.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            raise ValueError("notion_api_url must start with http:// or https://")
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
def notion_get_config(admin: AdminUser = Depends(get_current_admin)) -> dict:
    cfg, source = _load_config()
    return config_response(
        "notion", cfg, source,
        "DB values require NOTION_* env update + restart to apply" if source != "env" else "live env values in effect",
        effective_config=_env_config(),
    )


@router.put("/config")
def notion_put_config(req: NotionUpdateRequest, admin: AdminUser = Depends(require_l5)) -> dict:
    global _inmem
    saved = _read_saved_payload()
    cfg, _ = _load_config()
    if req.notion_api_url is not None:
        cfg["notion_api_url"] = req.notion_api_url
    if req.api_key is not None:
        cfg["api_key_set"] = True
        saved["encrypted_api_key"] = _encrypt_api_key(req.api_key)
        saved["secret_ref"] = _SECRET_REF
    saved.update(cfg)
    raw = json.dumps(saved)
    ok = _db_set_raw(raw, updated_by=getattr(admin, "email", None))
    if ok:
        _inmem = dict(saved)
        return config_response(
            "notion", cfg, "db",
            "saved; update NOTION_* env on the host and restart services to apply",
            effective_config=_env_config(),
        )
    if (os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod")):
        raise HTTPException(status_code=503, detail="Notion config DB unavailable in production (fail-closed)")
    _inmem = dict(saved)
    return config_response(
        "notion", cfg, "in-memory",
        "saved in-memory only (dev); update NOTION_* env and restart to apply",
        effective_config=_env_config(),
    )


@router.post("/test")
def notion_test(body: dict | None = None, admin: AdminUser = Depends(require_l5)) -> dict:
    """Probe Notion with the stored config (optional one-shot key override, never stored)."""
    cfg, source = _load_config()
    override = ""
    if isinstance(body, dict):
        override = str(body.get("api_key") or "").strip()
    key = resolve_api_key(override)
    if not key:
        return {"ok": False, "error": "no API key configured (save one first or pass api_key for a one-shot probe)", "source": source}
    target = cfg["notion_api_url"]
    t0 = time.monotonic()
    try:
        import httpx
        r = httpx.get(target + "/v1/users",
                      headers={"Authorization": f"Bearer {key}", "Notion-Version": NOTION_VERSION},
                      timeout=8.0)
        ms = round((time.monotonic() - t0) * 1000, 1)
        if r.status_code == 200:
            try:
                n = len(__import__("json").loads(r.text).get("results", []))
            except Exception:
                n = -1
            return {"ok": True, "target": target, "status_code": 200,
                    "user_count": n, "latency_ms": ms, "source": source}
        return {"ok": False, "target": target, "status_code": r.status_code,
                "error": r.text[:200], "latency_ms": ms, "source": source}
    except Exception as e:
        return {"ok": False, "target": target,
                "error": f"{type(e).__name__}: {str(e)[:160]}", "source": source}
