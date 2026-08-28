"""Admin Console — LLM Providers (frontend settings).

Routes:
- GET  /v1/llm/providers            — list (any authenticated admin)
- POST /v1/llm/providers            — create (L5 only)
- GET  /v1/llm/providers/{id}       — get single
- PATCH /v1/llm/providers/{id}      — update (L5)
- DELETE /v1/llm/providers/{id}     — delete (L5)
- POST /v1/llm/providers/{id}/test  — test connection (L5, mock)
- PATCH /v1/llm/providers/{id}/toggle — toggle enabled (L5) alternative POST /toggle

Provider types: claude, codex, gemini, opencode-go, openrouter, ollama
Field mapping:
  claude/codex/gemini -> apiKey (+ baseUrl optional, model)
  opencode-go         -> path (+ model optional)
  openrouter           -> apiKey (+ baseUrl optional, model)
  ollama              -> url (+ model)

Production-grade: Fernet encryption (OAOS_VAULT_KEY / VAULT_ENCRYPTION_KEY),
encrypted_api_key + secret_ref (vault://admin_llm_providers/{id}/api_key),
DB-backed (openagentos, SQLAlchemy, AdminLLMProviderORM) with in-memory fallback.
GET always returns masked apiKey (****), raw never leaked.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

try:
    from .auth import AdminUser, get_current_admin, require_l5
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/llm", tags=["llm"])


class ProviderType(str, Enum):
    claude = "claude"
    codex = "codex"
    gemini = "gemini"
    opencode_go = "opencode-go"
    openrouter = "openrouter"
    ollama = "ollama"

    # backward compat: opencode -> opencode-go
    @classmethod
    def _missing_(cls, value):
        if value == "opencode":
            return cls.opencode_go
        return None


_APIKEY_TYPES = {ProviderType.claude, ProviderType.codex, ProviderType.gemini, ProviderType.openrouter}

# ---------------------------------------------------------------------------
# Crypto helpers — Fernet, key from OAOS_VAULT_KEY / VAULT_ENCRYPTION_KEY
# ---------------------------------------------------------------------------
_DEV_VAULT_KEY = "dev-llm-provider-vault-key-please-change-32b"

_fernet_cache: dict[str, object] = {}  # key string -> Fernet


def _get_raw_vault_key() -> bytes:
    raw = os.environ.get("OAOS_VAULT_KEY") or os.environ.get("VAULT_ENCRYPTION_KEY") or ""
    raw = raw.strip()
    if not raw:
        if os.environ.get("OAOS_ENV", "").lower() in ("production", "prod"):
            raise RuntimeError("OAOS_VAULT_KEY/VAULT_ENCRYPTION_KEY must be set in production — refusing to use dev key (set a strong 32+ char key)")
        raw = _DEV_VAULT_KEY
    return raw.encode("utf-8")


def _derive_fernet_key(raw_key: bytes) -> bytes:
    digest = hashlib.sha256(raw_key).digest()
    return base64.urlsafe_b64encode(digest)


def _get_fernet():
    # cache per raw key value so env changes are reflected (test isolation)
    raw = _get_raw_vault_key()
    cache_key = raw.hex() if isinstance(raw, bytes) else str(raw)
    if cache_key in _fernet_cache:
        return _fernet_cache[cache_key]
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise RuntimeError("cryptography is required for Fernet encryption") from e
    fernet_key = _derive_fernet_key(raw)
    f = Fernet(fernet_key)
    _fernet_cache[cache_key] = f
    return f


def _encrypt_api_key(plain: str) -> str:
    if not plain:
        return ""
    f = _get_fernet()
    token = f.encrypt(plain.encode("utf-8"))
    return token.decode("utf-8")


def _decrypt_api_key(enc: str | None) -> str | None:
    if not enc:
        return None
    try:
        from cryptography.fernet import InvalidToken
    except ImportError:
        return None
    f = _get_fernet()
    try:
        plain = f.decrypt(enc.encode("utf-8"))
        return plain.decode("utf-8")
    except Exception:
        # InvalidToken or wrong key
        logger.warning("Failed to decrypt api_key — invalid token or wrong key")
        return None


def _make_secret_ref(provider_id: str) -> str:
    return f"vault://admin_llm_providers/{provider_id}/api_key"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class LLMProvider(BaseModel):
    id: str
    provider: ProviderType
    name: str = ""
    api_key: Optional[str] = None  # masked on output, plain in memory cache
    api_key_masked: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    path: Optional[str] = None
    url: Optional[str] = None
    enabled: bool = True
    created_at: datetime
    updated_at: datetime
    last_test_at: Optional[datetime] = None
    last_test_status: Optional[str] = None
    last_test_latency_ms: Optional[float] = None
    # vault metadata (not exposed raw, but available for audit)
    secret_ref: Optional[str] = None
    vault_backend: Optional[str] = None
    encrypted_api_key: Optional[str] = None


class LLMProviderCreate(BaseModel):
    provider: ProviderType
    name: Optional[str] = None
    apiKey: Optional[str] = Field(default=None, alias="api_key")
    api_key: Optional[str] = None
    baseUrl: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    path: Optional[str] = None
    url: Optional[str] = None
    enabled: Optional[bool] = True

    class Config:
        populate_by_name = True


class LLMProviderUpdate(BaseModel):
    provider: Optional[ProviderType] = None
    name: Optional[str] = None
    apiKey: Optional[str] = None
    api_key: Optional[str] = None
    baseUrl: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    path: Optional[str] = None
    url: Optional[str] = None
    enabled: Optional[bool] = None


def _mask_key(k: str | None) -> str | None:
    if not k:
        return None
    if len(k) <= 8:
        return "***"
    return k[:4] + "***" + k[-4:]


# ---------------------------------------------------------------------------
# In-memory store (fallback)
# ---------------------------------------------------------------------------
_providers: dict[str, LLMProvider] = {}
# encrypted storage for fallback verification (pid -> encrypted string)
_encrypted_store: dict[str, str] = {}
_secret_refs: dict[str, str] = {}


def clear_providers() -> None:
    _providers.clear()
    _encrypted_store.clear()
    _secret_refs.clear()
    _fernet_cache.clear()
    if _is_db_enabled():
        try:
            _db_clear_all()
        except Exception:
            pass


def _normalize_create(payload: LLMProviderCreate) -> dict:
    api_key = payload.apiKey if payload.apiKey is not None else payload.api_key
    base_url = payload.baseUrl if payload.baseUrl is not None else payload.base_url
    return {
        "provider": payload.provider,
        "name": (payload.name or "").strip(),
        "api_key": api_key,
        "base_url": base_url,
        "model": payload.model,
        "path": payload.path,
        "url": payload.url,
        "enabled": bool(payload.enabled) if payload.enabled is not None else True,
    }


def _validate_fields(provider: ProviderType, data: dict, is_update: bool = False) -> None:
    if provider in _APIKEY_TYPES:
        if not is_update and not data.get("api_key"):
            raise HTTPException(status_code=400, detail=f"apiKey is required for provider '{provider.value}'")
    elif provider == ProviderType.opencode_go:
        if not is_update and not data.get("path"):
            raise HTTPException(status_code=400, detail="path is required for provider 'opencode-go'")
    elif provider == ProviderType.ollama:
        if not is_update and not data.get("url"):
            raise HTTPException(status_code=400, detail="url is required for provider 'ollama'")


def _to_public(p: LLMProvider) -> dict:
    d = p.model_dump(mode="json")
    raw = d.get("api_key")
    # If api_key is already masked (contains ***), don't double-mask
    if raw and "***" in str(raw):
        masked = raw
    else:
        masked = _mask_key(raw)
    d["api_key_masked"] = masked
    d["apiKey"] = masked
    d["baseUrl"] = d.get("base_url")
    d["api_key"] = masked
    # expose secret_ref for audit (but never raw key)
    if p.secret_ref:
        d["secret_ref"] = p.secret_ref
    if p.vault_backend:
        d["vault_backend"] = p.vault_backend
    # never leak encrypted_api_key raw to frontend — remove
    d.pop("encrypted_api_key", None)
    return d


# ---------------------------------------------------------------------------
# DB helpers — lazy, sync SQLAlchemy, fallback to in-memory
# ---------------------------------------------------------------------------
_db_engine = None
_db_session_factory = None  # type: ignore


def _db_url() -> str | None:
    # priority: OAOS_DATABASE_URL then DATABASE_URL via persistence helper
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
    if url and url.strip():
        return url.strip()
    return None


def _is_db_enabled() -> bool:
    u = _db_url()
    return bool(u)


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


def _get_session_factory():
    global _db_engine, _db_session_factory
    if _db_session_factory is not None:
        return _db_session_factory
    url = _db_url()
    if not url:
        return None
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        sync_url = _normalize_sync_url(url)
        kwargs: dict = {"pool_pre_ping": True}
        if sync_url.startswith("sqlite"):
            kwargs = {}
            if ":memory:" in sync_url:
                kwargs["connect_args"] = {"check_same_thread": False}
        _db_engine = create_engine(sync_url, **kwargs)
        _db_session_factory = sessionmaker(bind=_db_engine, autoflush=False, autocommit=False)
        # ensure table exists
        _db_ensure_table(_db_engine)
        return _db_session_factory
    except Exception as e:
        logger.debug(f"LLM provider DB factory failed: {e}")
        return None


def _db_ensure_table(engine) -> None:
    try:
        from security.models.orm import AdminLLMProviderORM  # type: ignore
        from security.models.db import Base  # type: ignore

        Base.metadata.create_all(bind=engine)
        # ensure indexes exist (sqlite IF NOT EXISTS already in metadata; explicit for legacy)
        try:
            from sqlalchemy import text

            with engine.begin() as conn:
                try:
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_admin_llm_providers_provider ON admin_llm_providers (provider)"))
                except Exception:
                    pass
                try:
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_admin_llm_providers_secret_ref ON admin_llm_providers (secret_ref)"))
                except Exception:
                    pass
        except Exception:
            pass
        return
    except Exception:
        pass
    # fallback raw DDL (sqlite compat)
    try:
        from sqlalchemy import text

        ddl = """
        CREATE TABLE IF NOT EXISTS admin_llm_providers (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            encrypted_api_key TEXT,
            secret_ref TEXT,
            vault_backend TEXT,
            base_url TEXT,
            model TEXT,
            path TEXT,
            url TEXT,
            enabled BOOLEAN NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_test_at TEXT,
            last_test_status TEXT,
            last_test_latency_ms REAL,
            extra TEXT
        )
        """
        with engine.begin() as conn:
            conn.execute(text(ddl))
    except Exception:
        pass


def _orm_to_provider(row) -> LLMProvider:
    # decrypt api_key from encrypted_api_key
    enc = getattr(row, "encrypted_api_key", None)
    plain = _decrypt_api_key(enc) if enc else None
    created_at = getattr(row, "created_at")
    updated_at = getattr(row, "updated_at")
    # ensure tz aware
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if updated_at is not None and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    last_test_at = getattr(row, "last_test_at", None)
    if last_test_at is not None and last_test_at.tzinfo is None:
        last_test_at = last_test_at.replace(tzinfo=timezone.utc)
    return LLMProvider(
        id=str(row.id),
        provider=ProviderType(row.provider),
        name=str(getattr(row, "name", "") or ""),
        api_key=plain,
        api_key_masked=_mask_key(plain),
        base_url=getattr(row, "base_url", None),
        model=getattr(row, "model", None),
        path=getattr(row, "path", None),
        url=getattr(row, "url", None),
        enabled=bool(getattr(row, "enabled", True)),
        created_at=created_at,
        updated_at=updated_at,
        last_test_at=last_test_at,
        last_test_status=getattr(row, "last_test_status", None),
        last_test_latency_ms=getattr(row, "last_test_latency_ms", None),
        secret_ref=getattr(row, "secret_ref", None),
        vault_backend=getattr(row, "vault_backend", None),
        encrypted_api_key=enc,
    )


def _db_clear_all() -> None:
    factory = _get_session_factory()
    if factory is None:
        return
    try:
        from security.models.orm import AdminLLMProviderORM  # type: ignore

        with factory() as s:
            s.query(AdminLLMProviderORM).delete()
            s.commit()
    except Exception:
        pass


def _db_list_providers() -> list[LLMProvider] | None:
    if not _is_db_enabled():
        return None
    factory = _get_session_factory()
    if factory is None:
        return None
    try:
        from security.models.orm import AdminLLMProviderORM  # type: ignore

        with factory() as s:
            rows = s.query(AdminLLMProviderORM).order_by(AdminLLMProviderORM.created_at).all()
            return [_orm_to_provider(r) for r in rows]
    except Exception as e:
        logger.debug(f"DB list failed: {e}")
        return None


def _db_get_provider(pid: str) -> LLMProvider | None:
    if not _is_db_enabled():
        return None
    factory = _get_session_factory()
    if factory is None:
        return None
    try:
        from security.models.orm import AdminLLMProviderORM  # type: ignore

        with factory() as s:
            row = s.query(AdminLLMProviderORM).filter(AdminLLMProviderORM.id == pid).first()
            if row is None:
                return None
            return _orm_to_provider(row)
    except Exception:
        return None


def _db_create_provider(p: LLMProvider, encrypted_api_key: str | None, secret_ref: str | None) -> bool:
    if not _is_db_enabled():
        return False
    factory = _get_session_factory()
    if factory is None:
        return False
    try:
        from security.models.orm import AdminLLMProviderORM  # type: ignore

        with factory() as s:
            orm = AdminLLMProviderORM(
                id=p.id,
                provider=p.provider.value if hasattr(p.provider, "value") else str(p.provider),
                name=p.name or "",
                encrypted_api_key=encrypted_api_key,
                secret_ref=secret_ref,
                vault_backend=p.vault_backend or ("fernet" if encrypted_api_key else None),
                base_url=p.base_url,
                model=p.model,
                path=p.path,
                url=p.url,
                enabled=p.enabled,
                created_at=p.created_at,
                updated_at=p.updated_at,
                last_test_at=p.last_test_at,
                last_test_status=p.last_test_status,
                last_test_latency_ms=p.last_test_latency_ms,
            )
            s.add(orm)
            s.commit()
            return True
    except Exception as e:
        logger.debug(f"DB create failed: {e}")
        try:
            with factory() as s2:
                s2.rollback()
        except Exception:
            pass
        return False


def _db_update_provider(pid: str, updates: dict, encrypted_api_key: str | None = None, secret_ref: str | None = None) -> LLMProvider | None:
    if not _is_db_enabled():
        return None
    factory = _get_session_factory()
    if factory is None:
        return None
    try:
        from security.models.orm import AdminLLMProviderORM  # type: ignore

        with factory() as s:
            row = s.query(AdminLLMProviderORM).filter(AdminLLMProviderORM.id == pid).first()
            if row is None:
                return None
            for k, v in updates.items():
                if hasattr(v, "value"):
                    v = v.value
                setattr(row, k, v)
            if encrypted_api_key is not None:
                row.encrypted_api_key = encrypted_api_key
                row.secret_ref = secret_ref
                row.vault_backend = "fernet"
            # always bump updated_at if not in updates
            if "updated_at" not in updates:
                row.updated_at = datetime.now(timezone.utc)
            s.commit()
            s.refresh(row)
            return _orm_to_provider(row)
    except Exception as e:
        logger.debug(f"DB update failed: {e}")
        return None


def _db_delete_provider(pid: str) -> bool | None:
    if not _is_db_enabled():
        return None
    factory = _get_session_factory()
    if factory is None:
        return None
    try:
        from security.models.orm import AdminLLMProviderORM  # type: ignore

        with factory() as s:
            row = s.query(AdminLLMProviderORM).filter(AdminLLMProviderORM.id == pid).first()
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True
    except Exception:
        return None


def _db_persist_test_result(p: LLMProvider) -> None:
    if not _is_db_enabled():
        return
    factory = _get_session_factory()
    if factory is None:
        return
    try:
        from security.models.orm import AdminLLMProviderORM  # type: ignore

        with factory() as s:
            row = s.query(AdminLLMProviderORM).filter(AdminLLMProviderORM.id == p.id).first()
            if row is None:
                return
            row.last_test_at = p.last_test_at
            row.last_test_status = p.last_test_status
            row.last_test_latency_ms = p.last_test_latency_ms
            row.updated_at = p.updated_at
            s.commit()
    except Exception:
        pass


def _db_persist_toggle(p: LLMProvider) -> None:
    if not _is_db_enabled():
        return
    factory = _get_session_factory()
    if factory is None:
        return
    try:
        from security.models.orm import AdminLLMProviderORM  # type: ignore

        with factory() as s:
            row = s.query(AdminLLMProviderORM).filter(AdminLLMProviderORM.id == p.id).first()
            if row is None:
                return
            row.enabled = p.enabled
            row.updated_at = p.updated_at
            s.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers to fetch with DB fallback
# ---------------------------------------------------------------------------
def _list_all_providers() -> list[LLMProvider]:
    if _is_db_enabled():
        db_items = _db_list_providers()
        if db_items is not None:
            # sync in-memory cache for fast reads
            for item in db_items:
                _providers[item.id] = item
                if item.encrypted_api_key:
                    _encrypted_store[item.id] = item.encrypted_api_key
                if item.secret_ref:
                    _secret_refs[item.id] = item.secret_ref
            return db_items
    return sorted(_providers.values(), key=lambda x: x.created_at)


def _get_one_provider(pid: str) -> LLMProvider | None:
    if _is_db_enabled():
        db_item = _db_get_provider(pid)
        if db_item is not None:
            _providers[pid] = db_item
            if db_item.encrypted_api_key:
                _encrypted_store[pid] = db_item.encrypted_api_key
            if db_item.secret_ref:
                _secret_refs[pid] = db_item.secret_ref
            return db_item
        # if DB enabled but not found, check if it exists in memory fallback (should not)
        # also check DB miss vs error: try to distinguish by trying list
        # If DB says not found, return None (do not fallback to stale memory)
        # But if DB error returned None, fallback to memory
        # _db_get_provider returns None for both not-found and error — we need to check existence via factory
        # To avoid false fallback, check if DB is reachable: if we got None due to error, still try memory
        # So we allow memory fallback only if provider exists in memory
        if pid in _providers:
            return _providers[pid]
        return None
    return _providers.get(pid)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _check_hermes_mode_guard() -> None:
    """If runtime_mode==hermes, LLM provider CRUD is noop — per §16.1.2(8) arch spec."""
    try:
        try:
            from .runtime_mode import get_mode, RuntimeMode  # type: ignore
        except ImportError:
            from runtime_mode import get_mode, RuntimeMode  # type: ignore
        if get_mode() == RuntimeMode.hermes:
            raise HTTPException(
                status_code=409,
                detail={"code": "HERMES_MODE_NOOP", "message": "Hermes mode is active — LLM Multi-Provider settings are disabled. Model routing is delegated to Hermes Agent. Switch to llm mode to configure providers."},
            )
    except HTTPException:
        raise
    except Exception:
        pass


@router.get("/providers")
def list_providers(admin: AdminUser = Depends(get_current_admin)):
    _check_hermes_mode_guard()
    items = [_to_public(v) for v in _list_all_providers()]
    return {"providers": items, "items": items, "count": len(items), "total": len(items)}


@router.post("/providers", status_code=201)
def create_provider(payload: LLMProviderCreate, admin: AdminUser = Depends(require_l5)):
    _check_hermes_mode_guard()
    data = _normalize_create(payload)
    _validate_fields(data["provider"], data, is_update=False)
    pid = f"llm_{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)

    # encrypt api_key
    encrypted: str | None = None
    secret_ref: str | None = None
    vault_backend: str | None = None
    plain_key = data["api_key"]
    if plain_key:
        encrypted = _encrypt_api_key(plain_key)
        secret_ref = _make_secret_ref(pid)
        vault_backend = "fernet"

    provider = LLMProvider(
        id=pid,
        provider=data["provider"],
        name=data["name"],
        api_key=plain_key,
        base_url=data["base_url"],
        model=data["model"],
        path=data["path"],
        url=data["url"],
        enabled=data["enabled"],
        created_at=now,
        updated_at=now,
        secret_ref=secret_ref,
        vault_backend=vault_backend,
        encrypted_api_key=encrypted,
    )
    provider.api_key_masked = _mask_key(provider.api_key)

    # persist to DB if enabled, else in-memory
    db_ok = False
    if _is_db_enabled():
        db_ok = _db_create_provider(provider, encrypted, secret_ref)
        if not db_ok:
            logger.warning(f"DB create failed for {pid}, falling back to in-memory")

    # always keep in-memory cache in sync (fallback)
    _providers[pid] = provider
    if encrypted:
        _encrypted_store[pid] = encrypted
    if secret_ref:
        _secret_refs[pid] = secret_ref

    return _to_public(provider)


@router.get("/providers/{provider_id}")
def get_provider(provider_id: str, admin: AdminUser = Depends(get_current_admin)):
    p = _get_one_provider(provider_id)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")
    return _to_public(p)


@router.patch("/providers/{provider_id}")
def update_provider(provider_id: str, payload: LLMProviderUpdate, admin: AdminUser = Depends(require_l5)):
    _check_hermes_mode_guard()
    p = _get_one_provider(provider_id)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")
    new_provider = payload.provider if payload.provider is not None else p.provider
    api_key = payload.apiKey if payload.apiKey is not None else payload.api_key
    base_url = payload.baseUrl if payload.baseUrl is not None else payload.base_url
    updates: dict = {}
    db_updates: dict = {}
    if payload.provider is not None:
        updates["provider"] = payload.provider
        db_updates["provider"] = payload.provider.value if hasattr(payload.provider, "value") else str(payload.provider)
    if payload.name is not None:
        updates["name"] = payload.name
        db_updates["name"] = payload.name
    # api_key handling: ignore masked placeholder, encrypt if real new value
    encrypted_update: str | None = None
    secret_ref_update: str | None = None
    if api_key is not None:
        if api_key and "***" in api_key:
            pass  # ignore placeholder, keep existing
        else:
            updates["api_key"] = api_key
            # encrypt
            if api_key:
                encrypted_update = _encrypt_api_key(api_key)
                secret_ref_update = _make_secret_ref(provider_id)
                # ensure secret_ref exists even if previously no key
                if not p.secret_ref:
                    secret_ref_update = _make_secret_ref(provider_id)
            else:
                encrypted_update = ""  # clear? keep None
                secret_ref_update = p.secret_ref  # keep existing secret_ref but encrypted empty
                # Actually clearing api_key: set encrypted to None and secret_ref keep? spec says encrypted_api_key nullable
                encrypted_update = None
                # keep secret_ref as is or clear? keep for audit
            updates["encrypted_api_key"] = encrypted_update
            updates["secret_ref"] = secret_ref_update
            updates["vault_backend"] = "fernet" if encrypted_update else None
    if base_url is not None:
        updates["base_url"] = base_url
        db_updates["base_url"] = base_url
    if payload.model is not None:
        updates["model"] = payload.model
        db_updates["model"] = payload.model
    if payload.path is not None:
        updates["path"] = payload.path
        db_updates["path"] = payload.path
    if payload.url is not None:
        updates["url"] = payload.url
        db_updates["url"] = payload.url
    if payload.enabled is not None:
        updates["enabled"] = bool(payload.enabled)
        db_updates["enabled"] = bool(payload.enabled)

    if payload.provider is not None or api_key is not None or payload.path is not None or payload.url is not None:
        merged = {
            "api_key": updates.get("api_key", p.api_key),
            "path": updates.get("path", p.path),
            "url": updates.get("url", p.url),
        }
        _validate_fields(new_provider, merged, is_update=True)

    for k, v in updates.items():
        setattr(p, k, v)
    p.updated_at = datetime.now(timezone.utc)
    db_updates["updated_at"] = p.updated_at
    p.api_key_masked = _mask_key(p.api_key)

    # DB update if enabled
    if _is_db_enabled():
        # For api key, pass encrypted_update/secret_ref_update if changed
        if "api_key" in updates:
            # api key was updated (including clearing)
            _db_update_provider(provider_id, db_updates, encrypted_api_key=encrypted_update, secret_ref=secret_ref_update)
        else:
            if db_updates:
                _db_update_provider(provider_id, db_updates)
        # re-fetch to ensure consistency? Keep p as source of truth

    # update in-memory caches
    _providers[provider_id] = p
    if encrypted_update is not None:
        if encrypted_update:
            _encrypted_store[provider_id] = encrypted_update
        else:
            _encrypted_store.pop(provider_id, None)
    if secret_ref_update is not None and secret_ref_update:
        _secret_refs[provider_id] = secret_ref_update

    return _to_public(p)


@router.delete("/providers/{provider_id}")
def delete_provider(provider_id: str, admin: AdminUser = Depends(require_l5)):
    _check_hermes_mode_guard()
    # try DB first
    if _is_db_enabled():
        res = _db_delete_provider(provider_id)
        if res is True:
            _providers.pop(provider_id, None)
            _encrypted_store.pop(provider_id, None)
            _secret_refs.pop(provider_id, None)
            return {"status": "deleted", "id": provider_id}
        elif res is False:
            # DB says not found — check memory fallback
            if provider_id not in _providers:
                raise HTTPException(status_code=404, detail="provider not found")
            # else fall through to delete memory
        # res is None -> DB error, fallback to memory
    if provider_id not in _providers:
        raise HTTPException(status_code=404, detail="provider not found")
    del _providers[provider_id]
    _encrypted_store.pop(provider_id, None)
    _secret_refs.pop(provider_id, None)
    return {"status": "deleted", "id": provider_id}


@router.post("/providers/{provider_id}/test")
def test_provider(provider_id: str, request: Request, admin: AdminUser = Depends(require_l5)):
    _check_hermes_mode_guard()
    # --- quota guard (fail-open) ---
    tenant_id = request.headers.get("X-Tenant-Id") or request.headers.get("x-tenant-id") or request.query_params.get("tenant_id") or "default"
    try:
        _check_quota_or_raise(tenant_id)
    except HTTPException:
        raise
    except Exception:
        pass
    p = _get_one_provider(provider_id)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")
    start = time.perf_counter()
    time.sleep(0.05)
    latency = round((time.perf_counter() - start) * 1000, 1)
    ok = True
    reason = "ok"
    if p.provider in _APIKEY_TYPES and not p.api_key:
        ok = False
        reason = "missing apiKey"
    elif p.provider == ProviderType.opencode_go and not p.path:
        ok = False
        reason = "missing path"
    elif p.provider == ProviderType.ollama and not p.url:
        ok = False
        reason = "missing url"
    p.last_test_at = datetime.now(timezone.utc)
    p.last_test_status = "ok" if ok else "failed"
    p.last_test_latency_ms = latency
    p.updated_at = datetime.now(timezone.utc)
    _providers[provider_id] = p
    # persist to DB
    _db_persist_test_result(p)
    return {"status": "ok" if ok else "failed", "latency_ms": latency, "detail": reason, "provider_id": provider_id}


@router.post("/providers/{provider_id}/toggle")
@router.patch("/providers/{provider_id}/toggle")
def toggle_provider(provider_id: str, admin: AdminUser = Depends(require_l5)):
    _check_hermes_mode_guard()
    p = _get_one_provider(provider_id)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")
    p.enabled = not p.enabled
    p.updated_at = datetime.now(timezone.utc)
    _providers[provider_id] = p
    _db_persist_toggle(p)
    return _to_public(p)


# ---------------------------------------------------------------------------
# Tenant LLM quota (010) — fail-open
# ---------------------------------------------------------------------------
_quota_store: dict[str, dict] = {}
_quota_window_counts: dict[str, int] = {}

def _quota_tenant_key(tenant_id: str) -> str:
    return (tenant_id or "default").strip() or "default"

def clear_quotas() -> None:
    _quota_store.clear()
    _quota_window_counts.clear()
    if _is_db_enabled():
        try:
            factory = _get_session_factory()
            if factory is not None:
                from security.models.orm import AdminLLMQuotaORM
                with factory() as s:
                    s.query(AdminLLMQuotaORM).delete()
                    s.commit()
        except Exception:
            pass

def _ensure_quota_table(engine) -> None:
    try:
        from security.models.orm import AdminLLMQuotaORM  # noqa
        from security.models.db import Base
        Base.metadata.create_all(bind=engine)
    except Exception:
        pass
    try:
        from sqlalchemy import text
        ddl = """CREATE TABLE IF NOT EXISTS admin_llm_quotas (
            tenant_id TEXT PRIMARY KEY, daily_limit INTEGER NOT NULL DEFAULT 100,
            per_minute_limit INTEGER NOT NULL DEFAULT 10, used_today INTEGER NOT NULL DEFAULT 0,
            window_start TEXT, updated_at TEXT NOT NULL)"""
        with engine.begin() as conn:
            conn.execute(text(ddl))
    except Exception:
        pass

def _check_quota_or_raise(tenant_id: str) -> None:
    tid = _quota_tenant_key(tenant_id)
    now = datetime.now(timezone.utc)
    # try DB first (fail-open)
    try:
        if _is_db_enabled():
            factory = _get_session_factory()
            if factory is not None:
                from security.models.orm import AdminLLMQuotaORM
                # ensure table exists
                try:
                    _ensure_quota_table(factory.bind if hasattr(factory, "bind") else _db_engine)
                except Exception:
                    pass
                with factory() as s:
                    row = s.query(AdminLLMQuotaORM).filter(AdminLLMQuotaORM.tenant_id == tid).first()
                    if row is None:
                        row = AdminLLMQuotaORM(tenant_id=tid, daily_limit=100, per_minute_limit=10, used_today=0, window_start=now, updated_at=now)
                        s.add(row)
                        s.commit()
                        s.refresh(row)
                    # daily reset (UTC date)
                    if row.updated_at and row.updated_at.date() != now.date():
                        row.used_today = 0
                        row.window_start = now
                        _quota_window_counts[tid] = 0
                    # per-minute window
                    wc = _quota_window_counts.get(tid, 0)
                    ws = row.window_start
                    if ws is None or (now - (ws if ws.tzinfo else ws.replace(tzinfo=timezone.utc))).total_seconds() >= 60:
                        wc = 0
                        row.window_start = now
                    if row.used_today >= row.daily_limit:
                        raise HTTPException(status_code=429, detail={"code": "QUOTA_EXCEEDED", "message": "daily quota exceeded"})
                    if wc >= row.per_minute_limit:
                        raise HTTPException(status_code=429, detail={"code": "QUOTA_EXCEEDED", "message": "per-minute quota exceeded"})
                    row.used_today += 1
                    wc += 1
                    _quota_window_counts[tid] = wc
                    row.updated_at = now
                    s.commit()
                    return
    except HTTPException:
        raise
    except Exception:
        # fail-open on DB error
        pass
    # in-memory fallback
    rec = _quota_store.get(tid)
    if rec is None:
        rec = {"daily_limit": 100, "per_minute_limit": 10, "used_today": 0, "window_start": now, "updated_at": now}
        _quota_store[tid] = rec
        _quota_window_counts[tid] = 0
    if rec["updated_at"].date() != now.date():
        rec["used_today"] = 0
        rec["window_start"] = now
        _quota_window_counts[tid] = 0
    wc = _quota_window_counts.get(tid, 0)
    if (now - rec["window_start"]).total_seconds() >= 60:
        wc = 0
        rec["window_start"] = now
    if rec["used_today"] >= rec["daily_limit"]:
        raise HTTPException(status_code=429, detail={"code": "QUOTA_EXCEEDED", "message": "daily quota exceeded"})
    if wc >= rec["per_minute_limit"]:
        raise HTTPException(status_code=429, detail={"code": "QUOTA_EXCEEDED", "message": "per-minute quota exceeded"})
    rec["used_today"] += 1
    _quota_window_counts[tid] = wc + 1
    rec["updated_at"] = now
    # also try persist to DB best-effort if DB was unreachable earlier
    try:
        if _is_db_enabled():
            factory = _get_session_factory()
            if factory is not None:
                from security.models.orm import AdminLLMQuotaORM
                with factory() as s:
                    row = s.query(AdminLLMQuotaORM).filter(AdminLLMQuotaORM.tenant_id == tid).first()
                    if row is None:
                        row = AdminLLMQuotaORM(tenant_id=tid, daily_limit=rec["daily_limit"], per_minute_limit=rec["per_minute_limit"], used_today=rec["used_today"], window_start=rec["window_start"], updated_at=rec["updated_at"])
                        s.add(row)
                    else:
                        row.used_today = rec["used_today"]
                        row.window_start = rec["window_start"]
                        row.updated_at = rec["updated_at"]
                        row.daily_limit = rec["daily_limit"]
                        row.per_minute_limit = rec["per_minute_limit"]
                    s.commit()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Helpers for testing / inspection
# ---------------------------------------------------------------------------
def get_encrypted_api_key(provider_id: str) -> str | None:
    """Return encrypted ciphertext for a provider (for tests)."""
    if provider_id in _encrypted_store:
        return _encrypted_store[provider_id]
    # try DB
    if _is_db_enabled():
        factory = _get_session_factory()
        if factory is not None:
            try:
                from security.models.orm import AdminLLMProviderORM  # type: ignore

                with factory() as s:
                    row = s.query(AdminLLMProviderORM).filter(AdminLLMProviderORM.id == provider_id).first()
                    if row is not None:
                        return getattr(row, "encrypted_api_key", None)
            except Exception:
                pass
    return None


def get_secret_ref(provider_id: str) -> str | None:
    if provider_id in _secret_refs:
        return _secret_refs[provider_id]
    if _is_db_enabled():
        factory = _get_session_factory()
        if factory is not None:
            try:
                from security.models.orm import AdminLLMProviderORM  # type: ignore

                with factory() as s:
                    row = s.query(AdminLLMProviderORM).filter(AdminLLMProviderORM.id == provider_id).first()
                    if row is not None:
                        return getattr(row, "secret_ref", None)
            except Exception:
                pass
    return None


def decrypt_api_key_for_test(enc: str) -> str | None:
    return _decrypt_api_key(enc)
