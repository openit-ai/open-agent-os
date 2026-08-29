"""Fallback settings — LLM fallback chain config (admin-console/backend/fallback.py).

GET  /v1/llm/fallback — read fallback config (any authenticated admin)
PUT  /v1/llm/fallback — update fallback config (L5 only)

Ownership gate (runtime ownership separation):
  OAOS owns LLM Runtime fallback. Hermes Runtime fallback is authoritative on
  the commercial Hermes server and must NOT be managed by OAOS.
  When runtime_mode==hermes, all /v1/llm/fallback routes return 409
  HERMES_MODE_NOOP (same contract as /v1/llm/providers in hermes mode).
  The legacy Hermes config file writer (_write_hermes_config) is disabled
  and retained only as a no-op behind explicit opt-in env OAOS_ALLOW_HERMES_FALLBACK_WRITE=1
  (default OFF). OAOS never writes Hermes config by default.

Persists in DB admin_settings.llm_fallback (JSON) > env OAOS_LLM_FALLBACK_JSON > in-memory.
No secrets — only provider/model/order/enabled. Admin auth required on all routes.
Writes mirrored to env (OAOS_LLM_FALLBACK_JSON + legacy OAOS_FALLBACK_PROVIDERS/MODEL)
for OAOS Runtime consumers only — never to Hermes.

OAOS LLM Runtime deployments are preserved:
  When runtime_mode==llm, fallback CRUD + env mirroring remain fully functional.
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
router = APIRouter(prefix="/v1/llm", tags=["llm-fallback"])

ALLOWED_PROVIDERS = {"claude", "codex", "gemini", "opencode-go", "openrouter", "ollama", "opencode"}

# Normalize alias
def _normalize_provider(p: str) -> str:
    p = p.strip().lower()
    if p == "opencode":
        return "opencode-go"
    return p

class FallbackEntry(BaseModel):
    provider: str
    model: Optional[str] = None
    enabled: bool = True

    @field_validator("provider")
    @classmethod
    def check_provider(cls, v: str) -> str:
        nv = _normalize_provider(v)
        if nv not in ALLOWED_PROVIDERS and nv != "opencode-go":
            allowed = {"claude", "codex", "gemini", "opencode-go", "openrouter", "ollama"}
            if nv not in allowed:
                raise ValueError(f"provider must be one of {sorted(allowed)}")
        return nv

    @field_validator("model")
    @classmethod
    def check_model(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if v == "":
            return None
        if len(v) > 128:
            raise ValueError("model too long (max 128)")
        # Guard: block persisted custom gpt-5.6-luna / loopback fallbacks that must not override primary
        low = v.lower()
        if "gpt-5.6-luna" in low or "gpt-5.6-sol" in low or "gpt-5.6" in low:
            raise ValueError("model 'gpt-5.6-luna/sol' fallback is not allowed — misconfigured custom provider (blocked)")
        if "127.0.0.1:10100" in low or "localhost:10100" in low:
            raise ValueError("fallback model/base_url contains blocked loopback 127.0.0.1:10100")
        return v

class FallbackConfig(BaseModel):
    enabled: bool = True
    chain: list[FallbackEntry] = Field(default_factory=list)
    fallback_model: Optional[str] = None
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None

    @field_validator("fallback_model")
    @classmethod
    def check_fallback_model(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if v == "":
            return None
        if len(v) > 128:
            raise ValueError("fallback_model too long (max 128)")
        low = v.lower()
        if "gpt-5.6-luna" in low or "gpt-5.6-sol" in low or "gpt-5.6" in low:
            raise ValueError("fallback_model 'gpt-5.6-luna/sol' is not allowed — blocked custom provider")
        if "127.0.0.1:10100" in low or "localhost:10100" in low:
            raise ValueError("fallback_model contains blocked loopback 127.0.0.1:10100")
        return v

# rebuild for forward refs (Pydantic v2)
FallbackConfig.model_rebuild()

class FallbackUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    chain: Optional[list[FallbackEntry]] = None
    fallback_model: Optional[str] = None

    @field_validator("fallback_model")
    @classmethod
    def check_fallback_model(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if v == "":
            return None
        if len(v) > 128:
            raise ValueError("fallback_model too long (max 128)")
        low = v.lower()
        if "gpt-5.6-luna" in low or "gpt-5.6-sol" in low or "gpt-5.6" in low:
            raise ValueError("fallback_model 'gpt-5.6-luna/sol' is not allowed — blocked")
        if "127.0.0.1:10100" in low or "localhost:10100" in low:
            raise ValueError("fallback_model contains blocked loopback 127.0.0.1:10100")
        return v

# ---- ownership gate ----
def _is_hermes_mode() -> bool:
    """Return True when OAOS is in hermes mode — fallback is Hermes-owned."""
    try:
        try:
            from .runtime_mode import get_mode, RuntimeMode  # type: ignore
        except ImportError:
            from runtime_mode import get_mode, RuntimeMode  # type: ignore
        return get_mode() == RuntimeMode.hermes
    except Exception:
        # fail-open to allow tests without runtime_mode module? But log.
        # If runtime_mode unavailable, assume not hermes (preserve OAOS).
        return False

def _check_fallback_ownership_guard() -> None:
    """Enforce runtime ownership gate. Raises 409 when Hermes owns fallback."""
    if _is_hermes_mode():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "HERMES_MODE_NOOP",
                "message": "Fallback management is disabled in hermes mode — Hermes Runtime owns LLM routing. Switch to llm mode to configure OAOS fallback chain.",
            },
        )

# ---- persistence helpers ----
_db_engine = None

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
        sync_url = _normalize_sync_url(url)
        kwargs: dict = {"pool_pre_ping": True}
        if sync_url.startswith("sqlite"):
            kwargs = {}
            if ":memory:" in sync_url:
                kwargs["connect_args"] = {"check_same_thread": False}
        _db_engine = create_engine(sync_url, **kwargs)
        return _db_engine
    except Exception as e:
        logger.debug(f"fallback DB engine failed: {e}")
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
            row = conn.execute(text("SELECT value FROM admin_settings WHERE key='llm_fallback'")).fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logger.debug(f"fallback DB read failed: {e}")
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
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES ('llm_fallback', :v, :now, :by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception:
                pass
            try:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES ('llm_fallback', :v, :now, :by)"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception as e2:
                logger.debug(f"fallback DB write fallback failed: {e2}")
                return False
    except Exception as e:
        logger.debug(f"fallback DB write failed: {e}")
        return False

# in-memory
_inmem: FallbackConfig | None = None

def _load_config() -> FallbackConfig:
    global _inmem
    # Helper to strip blocked entries (custom/gpt-5.6-luna/127.0.0.1:10100) that may have been persisted before guard
    def _strip_blocked(data: dict) -> dict:
        try:
            chain = data.get("chain")
            if isinstance(chain, list):
                kept = []
                for item in chain:
                    if not isinstance(item, dict):
                        continue
                    prov = str(item.get("provider", "")).lower()
                    model = str(item.get("model", "")).lower()
                    # check provider allowlist + blocked model substrings
                    if prov == "custom":
                        logger.warning("[model_guard] _load_config stripping blocked chain entry provider=custom model=%r", item.get("model"))
                        continue
                    if prov not in ALLOWED_PROVIDERS and prov not in ("opencode",):
                        # also blocked if provider not allowlisted (fail-closed)
                        if prov not in ("", "hermes", "safe"):
                            logger.warning("[model_guard] _load_config stripping disallowed provider chain entry %r", item)
                            continue
                    if "gpt-5.6-luna" in model or "gpt-5.6-sol" in model or "gpt-5.6" in model or "127.0.0.1:10100" in model or "localhost:10100" in model:
                        logger.warning("[model_guard] _load_config stripping blocked model in chain %r", item)
                        continue
                    # also check base_url if present
                    burl = str(item.get("base_url", item.get("baseUrl", ""))).lower()
                    if "127.0.0.1:10100" in burl or "localhost:10100" in burl:
                        logger.warning("[model_guard] _load_config stripping blocked base_url in chain %r", item)
                        continue
                    kept.append(item)
                data = dict(data)
                data["chain"] = kept
            fm = str(data.get("fallback_model", "") or "")
            low = fm.lower()
            if "gpt-5.6-luna" in low or "gpt-5.6-sol" in low or "gpt-5.6" in low or "127.0.0.1:10100" in low or "localhost:10100" in low:
                logger.warning("[model_guard] _load_config stripping blocked fallback_model=%r", fm)
                data = dict(data)
                data["fallback_model"] = None
        except Exception:
            pass
        return data
    # DB first
    raw = _db_get_raw()
    if raw:
        try:
            data = json.loads(raw)
            data = _strip_blocked(data)
            cfg = FallbackConfig(**data)
            _inmem = cfg
            # mirror to env for OAOS consumers (LLM Runtime only)
            os.environ["OAOS_LLM_FALLBACK_JSON"] = raw
            return cfg
        except Exception as e:
            logger.debug(f"fallback parse DB failed: {e}")
    # env
    env_raw = os.environ.get("OAOS_LLM_FALLBACK_JSON", "").strip()
    if env_raw:
        try:
            data = json.loads(env_raw)
            data = _strip_blocked(data)
            cfg = FallbackConfig(**data)
            _inmem = cfg
            return cfg
        except Exception:
            pass
    # in-memory or default
    if _inmem is not None:
        return _inmem
    # default: enabled true, empty chain (no fallback)
    cfg = FallbackConfig(enabled=True, chain=[], fallback_model=None)
    _inmem = cfg
    return cfg

def _save_config(cfg: FallbackConfig, updated_by: str | None = None) -> FallbackConfig:
    global _inmem
    cfg.updated_at = datetime.now(timezone.utc).isoformat()
    if updated_by:
        cfg.updated_by = updated_by
    raw = cfg.model_dump_json()
    _inmem = cfg
    os.environ["OAOS_LLM_FALLBACK_JSON"] = raw
    # also set legacy env for OAOS consumers only (not Hermes)
    try:
        chain_env = ",".join([e.provider for e in cfg.chain if e.enabled])
        if chain_env:
            os.environ["OAOS_FALLBACK_PROVIDERS"] = chain_env
        else:
            os.environ.pop("OAOS_FALLBACK_PROVIDERS", None)
        if cfg.fallback_model:
            os.environ["OAOS_FALLBACK_MODEL"] = cfg.fallback_model
        else:
            os.environ.pop("OAOS_FALLBACK_MODEL", None)
    except Exception:
        pass
    _db_set_raw(raw, updated_by=updated_by)
    # Hermes config write is disabled by default (ownership gate).
    # Retained only behind explicit opt-in env for legacy migration.
    _write_hermes_config(cfg)
    return cfg

def _is_hermes_owned() -> bool:
    """Alias for _is_hermes_mode — kept for test/compat."""
    return _is_hermes_mode()

def _is_hermes_config_mirror_allowed() -> bool:
    """Mirror allowed only when LLM owns fallback AND explicit opt-in is set."""
    if _is_hermes_mode():
        return False
    allow = (
        os.environ.get("OAOS_ALLOW_HERMES_FALLBACK_WRITE", "").strip().lower()
        or os.environ.get("OAOS_ALLOW_HERMES_CONFIG_WRITE", "").strip().lower()
    )
    return allow in ("1", "true", "yes", "on")

def _write_hermes_config(cfg: FallbackConfig) -> None:
    """Hermes-owned fallback management is disabled in OAOS.

    This function is a deliberate NO-OP unless explicitly enabled via
    OAOS_ALLOW_HERMES_FALLBACK_WRITE=1 (or legacy OAOS_ALLOW_HERMES_CONFIG_WRITE=1).
    Hermes Runtime is authoritative on the commercial server; OAOS must never
    overwrite its config silently.  Keeping this stub preserves import
    compatibility while enforcing ownership separation.
    """
    if not _is_hermes_config_mirror_allowed():
        logger.debug("Hermes fallback write skipped (ownership gate: allow flag not set or hermes mode)")
        return
    path = os.environ.get("OAOS_HERMES_CONFIG_PATH") or os.environ.get("HERMES_CONFIG_PATH")
    if not path:
        logger.debug("Hermes fallback write skipped: no config path set")
        return
    try:
        import pathlib
        p = pathlib.Path(path)
        if not p.exists():
            logger.debug(f"Hermes fallback write skipped: path does not exist: {path}")
            return
        content = p.read_text(encoding="utf-8")
        data = json.loads(content) if content.strip() else {}
        data["fallback"] = {
            "enabled": cfg.enabled,
            "chain": [e.model_dump() for e in cfg.chain],
            "fallback_model": cfg.fallback_model,
        }
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.warning(f"Hermes fallback config overwritten via OAOS (explicit opt-in): {path}")
    except Exception as e:
        logger.debug(f"hermes config write skipped: {e}")

def _validate_update(req: FallbackUpdateRequest, current: FallbackConfig) -> FallbackConfig:
    enabled = req.enabled if req.enabled is not None else current.enabled
    chain = req.chain if req.chain is not None else current.chain
    fallback_model = req.fallback_model if req.fallback_model is not None else current.fallback_model
    if req.fallback_model is not None and req.fallback_model == "":
        fallback_model = None
    if req.fallback_model is not None and req.fallback_model.strip() == "":
        fallback_model = None

    if len(chain) > 20:
        raise HTTPException(status_code=400, detail="chain too long (max 20)")
    return FallbackConfig(enabled=enabled, chain=chain, fallback_model=fallback_model, updated_at=current.updated_at, updated_by=current.updated_by)

@router.get("/fallback")
def get_fallback(admin: AdminUser = Depends(get_current_admin)):
    _check_fallback_ownership_guard()
    cfg = _load_config()
    return cfg.model_dump(mode="json")

@router.put("/fallback")
def put_fallback(body: FallbackUpdateRequest, admin: AdminUser = Depends(require_l5)):
    _check_fallback_ownership_guard()
    current = _load_config()
    new_cfg = _validate_update(body, current)
    saved = _save_config(new_cfg, updated_by=admin.email)
    return saved.model_dump(mode="json")

# Compat: POST also allowed
@router.post("/fallback")
def post_fallback(body: FallbackUpdateRequest, admin: AdminUser = Depends(require_l5)):
    _check_fallback_ownership_guard()
    current = _load_config()
    new_cfg = _validate_update(body, current)
    saved = _save_config(new_cfg, updated_by=admin.email)
    return saved.model_dump(mode="json")

def clear_fallback_cache() -> None:
    global _inmem, _db_engine
    _inmem = None
    if _db_engine is not None:
        try:
            _db_engine.dispose()
        except Exception:
            pass
    _db_engine = None
