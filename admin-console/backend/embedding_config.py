"""Embedding config console surface (admin-console/backend/embedding_config.py).

GET  /v1/embedding/config — read effective embedding config (auth)
PUT  /v1/embedding/config — L5 update (admin_settings.embedding_config JSON)

Precedence: DB embedding_config JSON > OAOS_EMBED_*/OLLAMA_* env > defaults.
The worker reads OAOS_EMBED_* env at startup, so DB values are the console
source of truth and applying requires a host env update + restart
(restart_required=true, applied=false unless source==env).

HARD RULE: the real embedding call path (knowledge_index/embedding.py,
memory_service) is NEVER touched here — this module only stores display
config. Secrets/keys are never accepted or returned by this module.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

try:
    from .auth import AdminUser, get_current_admin, require_l5
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/embedding", tags=["embedding"])

EMBEDDING_KEY = "embedding_config"

DEFAULT_PROVIDER = "ollama"
DEFAULT_MODEL = "bge-m3:latest"
DEFAULT_API_URL = "http://127.0.0.1:11434"
DEFAULT_DIM = 1024

ALLOWED_PROVIDERS = ("ollama", "openai-compatible", "fake")

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
        logger.debug(f"embedding DB engine failed: {e}")
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
            row = conn.execute(text("SELECT value FROM admin_settings WHERE key='embedding_config'")).fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logger.debug(f"embedding DB read failed: {e}")
    return None


def _db_set_raw(value_json: str, updated_by: str | None = None) -> bool:
    try:
        engine = _get_engine()
        if engine is None:
            return False
        _ensure_table(engine)
        from sqlalchemy import text
        now = datetime.now(timezone.utc).isoformat()
        with engine.begin() as conn:
            try:
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES ('embedding_config', :v, :now, :by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception:
                pass
            try:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES ('embedding_config', :v, :now, :by)"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception as e2:
                logger.debug(f"embedding DB write fallback failed: {e2}")
                return False
    except Exception as e:
        logger.debug(f"embedding DB write failed: {e}")
        return False


def _env_str(*names: str) -> str:
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


def _env_config() -> dict:
    dim = DEFAULT_DIM
    raw_dim = _env_str("OAOS_EMBED_DIM", "OAOS_EMBEDDING_DIM")
    if raw_dim:
        try:
            dim = int(raw_dim)
        except Exception:
            dim = DEFAULT_DIM
    return {
        "provider": _env_str("OAOS_EMBED_PROVIDER", "OAOS_EMBEDDING_PROVIDER") or DEFAULT_PROVIDER,
        "model": _env_str("OAOS_EMBED_MODEL", "OAOS_EMBEDDING_MODEL", "OLLAMA_EMBED_MODEL", "OLLAMA_MODEL") or DEFAULT_MODEL,
        "dim": dim,
        "api_url": _env_str("OAOS_EMBED_API_URL", "OAOS_EMBEDDING_API_URL", "OLLAMA_API_URL", "OLLAMA_HOST") or DEFAULT_API_URL,
    }


def _load_config() -> tuple[dict, str]:
    global _inmem
    raw = _db_get_raw()
    if raw:
        try:
            data = json.loads(raw)
            env = _env_config()
            cfg = {
                "provider": str(data.get("provider") or env["provider"]),
                "model": str(data.get("model") or env["model"]),
                "dim": int(data.get("dim") or env["dim"]),
                "api_url": str(data.get("api_url") or env["api_url"]),
            }
            _inmem = cfg
            return cfg, "db"
        except Exception as e:
            logger.debug(f"embedding parse DB failed: {e}")
    if _inmem is not None:
        return dict(_inmem), "in-memory"
    return _env_config(), "env"


def _check_api_url(v: str) -> str:
    v = v.strip().rstrip("/")
    if not v:
        raise ValueError("api_url must not be empty")
    low = v.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        raise ValueError("api_url must start with http:// or https://")
    # Same guard as knowledge_index/embedding.py (read-only copy — that file is untouched)
    if "chroma" in low:
        raise ValueError("ChromaDB is forbidden — use Ollama 127.0.0.1:11434 only")
    if ":8000" in v:
        raise ValueError("port 8000 (ChromaDB) is forbidden — api_url must be Ollama 127.0.0.1:11434")
    return v


class EmbeddingUpdateRequest(BaseModel):
    provider: Optional[str] = Field(default=None, max_length=64)
    model: Optional[str] = Field(default=None, max_length=128)
    dim: Optional[int] = Field(default=None, ge=64, le=8192)
    api_url: Optional[str] = Field(default=None, max_length=512)

    @field_validator("provider")
    @classmethod
    def check_provider(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in ALLOWED_PROVIDERS:
            raise ValueError(f"provider must be one of {list(ALLOWED_PROVIDERS)}")
        return v

    @field_validator("model")
    @classmethod
    def check_model(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("model must not be empty")
        return v

    @field_validator("api_url")
    @classmethod
    def check_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _check_api_url(v)


_RESTART_NOTE = ("DB values require OAOS_EMBED_* env update on the host + restart "
                 "to apply. The live embedding call path is unchanged.")


@router.get("/config")
def embedding_get_config(admin: AdminUser = Depends(get_current_admin)) -> dict:
    cfg, source = _load_config()
    return {**cfg, "source": source, "applied": source == "env",
            "restart_required": source != "env",
            "note": _RESTART_NOTE if source != "env" else "live env values in effect"}


@router.put("/config")
def embedding_put_config(req: EmbeddingUpdateRequest,
                         admin: AdminUser = Depends(require_l5)) -> dict:
    global _inmem
    cfg, _ = _load_config()
    if req.provider is not None:
        cfg["provider"] = req.provider
    if req.model is not None:
        cfg["model"] = req.model
    if req.dim is not None:
        cfg["dim"] = req.dim
    if req.api_url is not None:
        cfg["api_url"] = req.api_url
    raw = json.dumps(cfg)
    ok = _db_set_raw(raw, updated_by=getattr(admin, "email", None))
    if ok:
        _inmem = dict(cfg)
        return {**cfg, "source": "db", "applied": False, "restart_required": True,
                "note": "saved; " + _RESTART_NOTE}
    if os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod"):
        raise HTTPException(status_code=503, detail="Embedding config DB unavailable in production (fail-closed)")
    _inmem = dict(cfg)
    return {**cfg, "source": "in-memory", "applied": False, "restart_required": True,
            "note": "saved in-memory only (dev); " + _RESTART_NOTE}
