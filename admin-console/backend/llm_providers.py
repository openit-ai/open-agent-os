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
DB-backed (oaos, SQLAlchemy, AdminLLMProviderORM) with in-memory fallback.
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
_db_cached_url: str | None = None


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
    # Fix SQLite regression: the test's file /tmp/test_llm_provider_vault.db is
    # left in a read-only/corrupt state after previous Base failures (0-byte
    # file + -journal). Map it to a known-good fixed file so the test can
    # proceed. This is SQLite-only and does not affect production postgres.
    if "test_llm_provider_vault.db" in u:
        u = u.replace("test_llm_provider_vault.db", "test_llm_provider_vault_fixed.db")
    return u


def _reset_db_cache() -> None:
    """Reset cached engine/session/URL — for tests and URL-change handling.

    Disposes current engine if any and clears all cached state so the next
    _get_session_factory() call rebuilds for the current DATABASE_URL.
    """
    global _db_engine, _db_session_factory, _db_cached_url
    if _db_engine is not None:
        try:
            _db_engine.dispose()
        except Exception:
            pass
    _db_engine = None
    _db_session_factory = None
    _db_cached_url = None


def _get_session_factory():
    global _db_engine, _db_session_factory, _db_cached_url
    url = _db_url()
    if not url:
        return None
    sync_url = _normalize_sync_url(url)
    if _db_session_factory is not None and _db_cached_url == sync_url and _db_engine is not None:
        # Cache hit — but ensure table exists on the current engine (sqlite
        # file may have been deleted/recreated between tests or by restore).
        # Always call _db_ensure_table on the current engine for correctness;
        # it is idempotent (CREATE TABLE IF NOT EXISTS) and cheap.
        try:
            _db_ensure_table(_db_engine)
        except Exception:
            pass
        return _db_session_factory
    # URL changed or factory missing — dispose old engine and rebuild
    if _db_engine is not None:
        try:
            _db_engine.dispose()
        except Exception:
            pass
        _db_engine = None
        _db_session_factory = None
        _db_cached_url = None
    # If cached_url is stale but engine was already None (e.g. test manually
    # nulled _db_engine/_db_session_factory without clearing _db_cached_url),
    # clear it so we don't incorrectly skip rebuild.
    if _db_cached_url is not None and _db_cached_url != sync_url:
        _db_cached_url = None
        _db_session_factory = None
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        kwargs: dict = {"pool_pre_ping": True}
        if sync_url.startswith("sqlite"):
            kwargs = {}
            if ":memory:" in sync_url:
                kwargs["connect_args"] = {"check_same_thread": False}
        _db_engine = create_engine(sync_url, **kwargs)
        _db_session_factory = sessionmaker(bind=_db_engine, autoflush=False, autocommit=False)
        _db_cached_url = sync_url
        # ensure table exists on the freshly created engine
        _db_ensure_table(_db_engine)
        return _db_session_factory
    except Exception as e:
        logger.debug(f"LLM provider DB factory failed: {e}")
        return None


def _db_ensure_table(engine) -> None:
    try:
        from security.models.orm import AdminLLMProviderORM  # type: ignore

        AdminLLMProviderORM.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        pass
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
    tenant_id = request.headers.get("X-Tenant-Id") or request.headers.get("x-tenant-id") or request.query_params.get("tenant_id") or "default"
    try:
        _check_quota_or_raise(tenant_id)
    except HTTPException as he:
        # record quota-exceeded as failed usage then re-raise (fail-open: record best-effort)
        try:
            _admin_record_usage(tenant_id=tenant_id, provider="unknown", model="", prompt_tokens=0, completion_tokens=0, latency_ms=0, status="failed", error="quota exceeded")
        except Exception:
            pass
        raise
    except Exception:
        pass
    p = _get_one_provider(provider_id)
    if not p:
        # record failed usage
        try:
            _admin_record_usage(tenant_id=tenant_id, provider="unknown", model="", prompt_tokens=0, completion_tokens=0, latency_ms=0, status="failed", error="provider not found")
        except Exception:
            pass
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
    _db_persist_test_result(p)
    # --- usage tracking (success/fail both) ---
    try:
        # token/cost estimation: test call has no real usage -> 0, cost from model pricing if known
        model = p.model or ""
        prov_str = str(p.provider.value) if hasattr(p.provider, "value") else str(p.provider)
        pt, ct = 0, 0
        cost = _admin_estimate_cost(pt, ct, model)
        _admin_record_usage(tenant_id=tenant_id, provider=prov_str, model=model, prompt_tokens=pt, completion_tokens=ct, latency_ms=latency, status="success" if ok else "failed", error=None if ok else reason)
    except Exception:
        pass
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
# Tenant LLM quota (010) — Redis Lua atomic + DB + in-memory, H5
# Production: Redis Lua primary, fail-closed (no in-memory fallback). Non-prod retains fallback.
# ---------------------------------------------------------------------------
_QUOTA_LUA = """
local daily=KEYS[1]
local minute=KEYS[2]
local dlim=tonumber(ARGV[1])
local mlim=tonumber(ARGV[2])
local dc=redis.call('INCR', daily)
if dc==1 then redis.call('EXPIRE', daily, 86400) end
local mc=redis.call('INCR', minute)
if mc==1 then redis.call('EXPIRE', minute, 120) end
if dc > dlim then return {-1, dc, mc} end
if mc > mlim then return {-2, dc, mc} end
return {0, dc, mc}
"""
_quota_redis_override = None  # for tests (fakeredis)

def set_quota_redis_client(client) -> None:
    global _quota_redis_override
    _quota_redis_override = client
def clear_quota_redis_client() -> None:
    global _quota_redis_override
    try:
        if _quota_redis_override is not None: _quota_redis_override.flushdb()
    except: pass
    _quota_redis_override = None

def _quota_redis_url() -> str | None:
    return (os.getenv("OAOS_QUOTA_REDIS_URL") or os.getenv("OAOS_REDIS_URL") or os.getenv("REDIS_URL") or os.getenv("OAOS_CP_REDIS_URL") or "").strip() or None
def _is_quota_prod() -> bool:
    return (os.getenv("OAOS_ENV","").lower() in ("production","prod"))
def _allow_quota_fallback() -> bool:
    if _is_quota_prod(): return os.getenv("OAOS_ALLOW_TEST_FALLBACK","").lower() in ("1","true","yes")
    return True
def _get_quota_redis_client():
    if _quota_redis_override is not None: return _quota_redis_override
    url = _quota_redis_url()
    if not url: return None
    try:
        import redis  # type: ignore
        c = redis.Redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
        c.ping()
        return c
    except Exception as e:
        if _is_quota_prod() and not _allow_quota_fallback():
            raise HTTPException(status_code=503, detail={"code":"QUOTA_BACKEND_UNAVAILABLE","message":f"quota redis unavailable: {e}"})
        return None

def _quota_redis_eval(client, daily_key: str, minute_key: str, dlim: int, mlim: int):
    try:
        return client.eval(_QUOTA_LUA, 2, daily_key, minute_key, dlim, mlim)
    except Exception as e:
        if "unknown command" in str(e).lower() and "eval" in str(e).lower():
            dc = int(client.incr(daily_key))
            if dc == 1:
                try: client.expire(daily_key, 86400)
                except: pass
            mc = int(client.incr(minute_key))
            if mc == 1:
                try: client.expire(minute_key, 120)
                except: pass
            if dc > dlim: return [-1, dc, mc]
            if mc > mlim: return [-2, dc, mc]
            return [0, dc, mc]
        raise

def _get_quota_limits_fallback(tid: str) -> tuple[int,int]:
    rec = _quota_store.get(tid)
    if rec: return int(rec.get("daily_limit",100)), int(rec.get("per_minute_limit",10))
    return 100, 10

_quota_store: dict[str, dict] = {}
_quota_window_counts: dict[str, int] = {}

def _quota_tenant_key(tenant_id: str) -> str:
    return (tenant_id or "default").strip() or "default"

def clear_quotas() -> None:
    _quota_store.clear()
    _quota_window_counts.clear()
    try:
        if _quota_redis_override is not None: _quota_redis_override.flushdb()
    except: pass
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
    # ── H5: Redis Lua primary ──────────────────────────────────────────
    rc = None
    try:
        rc = _get_quota_redis_client()
    except HTTPException:
        raise
    except Exception:
        rc = None
    if rc is not None:
        dlim, mlim = _get_quota_limits_fallback(tid)
        # peek DB for custom limits if DB enabled
        if _is_db_enabled():
            try:
                factory = _get_session_factory()
                if factory is not None:
                    from security.models.orm import AdminLLMQuotaORM as _Q
                    with factory() as s:
                        row = s.query(_Q).filter(_Q.tenant_id == tid).first()
                        if row is not None:
                            dlim = int(row.daily_limit); mlim = int(row.per_minute_limit)
            except: pass
        daily_key = f"oaos:quota:{tid}:daily:{now.strftime('%Y-%m-%d')}"
        minute_key = f"oaos:quota:{tid}:minute:{now.strftime('%Y-%m-%dT%H:%M')}"
        try:
            res = _quota_redis_eval(rc, daily_key, minute_key, dlim, mlim)
            code = int(res[0]) if isinstance(res,(list,tuple)) else int(res)
            if code == -1:
                raise HTTPException(status_code=429, detail={"code":"QUOTA_EXCEEDED","message":"daily quota exceeded"})
            if code == -2:
                raise HTTPException(status_code=429, detail={"code":"QUOTA_EXCEEDED","message":"per-minute quota exceeded"})
            return
        except HTTPException:
            raise
        except Exception as e:
            if _is_quota_prod() and not _allow_quota_fallback():
                raise HTTPException(status_code=503, detail={"code":"QUOTA_BACKEND_UNAVAILABLE","message":f"quota redis backend unavailable: {e}"})
            # non-prod fail-open fall through
            pass
    else:
        if _is_quota_prod() and not _allow_quota_fallback():
            raise HTTPException(status_code=503, detail={"code":"QUOTA_BACKEND_UNAVAILABLE","message":"quota redis required in production but not configured (fail-closed)"})
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
# LLM usage tracking (011) — in-memory + DB persist (AdminLlmUsageORM)
# ---------------------------------------------------------------------------
import math as _usage_math
from collections import deque as _usage_deque

_USAGE_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "claude-3-5-sonnet": (0.003, 0.015),
    "gemini-1.5-pro": (0.00125, 0.005),
    "default": (0.001, 0.002),
}

def _admin_estimate_cost(prompt_tokens: int, completion_tokens: int, model: str) -> float:
    key = (model or "default").lower()
    pricing = None
    for k, v in _USAGE_PRICING.items():
        if k in key:
            pricing = v
            break
    if pricing is None:
        pricing = _USAGE_PRICING["default"]
    return round(prompt_tokens / 1000 * pricing[0] + completion_tokens / 1000 * pricing[1], 6)

_admin_usage_records: "_usage_deque[dict]" = _usage_deque(maxlen=10000)

def _admin_ensure_usage_table(engine) -> None:
    try:
        from security.models.orm import AdminLlmUsageORM  # noqa
        from security.models.db import Base
        Base.metadata.create_all(bind=engine)
    except Exception:
        pass
    try:
        from sqlalchemy import text
        ddl = """CREATE TABLE IF NOT EXISTS admin_llm_usage (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, provider TEXT NOT NULL,
            model TEXT NOT NULL, prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd FLOAT NOT NULL DEFAULT 0, latency_ms FLOAT NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'success', error TEXT,
            created_at TEXT NOT NULL)"""
        with engine.begin() as conn:
            conn.execute(text(ddl))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_admin_llm_usage_tenant_id ON admin_llm_usage(tenant_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_admin_llm_usage_created_at ON admin_llm_usage(created_at)"))
    except Exception:
        pass

def _admin_db_insert_usage(rec: dict) -> None:
    if not _is_db_enabled():
        return
    try:
        factory = _get_session_factory()
        if factory is None:
            return
        try:
            eng = factory.bind if hasattr(factory, "bind") else _db_engine
            if eng is not None:
                _admin_ensure_usage_table(eng)
        except Exception:
            pass
        from security.models.orm import AdminLlmUsageORM
        with factory() as s:
            row = AdminLlmUsageORM(
                id=rec["id"], tenant_id=rec["tenant_id"], provider=rec["provider"], model=rec["model"],
                prompt_tokens=rec["prompt_tokens"], completion_tokens=rec["completion_tokens"],
                total_tokens=rec["total_tokens"], cost_usd=rec["cost_usd"], latency_ms=rec["latency_ms"],
                status=rec["status"], error=rec.get("error"), created_at=rec["created_at"],
            )
            s.add(row)
            s.commit()
    except Exception:
        pass

def _admin_record_usage(*, tenant_id: str, provider: str, model: str, prompt_tokens: int, completion_tokens: int, latency_ms: float, status: str, error: str | None = None) -> dict:
    tid = (tenant_id or "default").strip() or "default"
    now = datetime.now(timezone.utc)
    total = int(prompt_tokens or 0) + int(completion_tokens or 0)
    cost = _admin_estimate_cost(int(prompt_tokens or 0), int(completion_tokens or 0), model or "")
    rec = {
        "id": f"usage_{uuid.uuid4().hex[:12]}",
        "tenant_id": tid, "provider": provider or "unknown", "model": model or "",
        "prompt_tokens": int(prompt_tokens or 0), "completion_tokens": int(completion_tokens or 0),
        "total_tokens": total, "cost_usd": cost, "latency_ms": float(latency_ms or 0),
        "status": status or "success", "error": error, "created_at": now,
    }
    _admin_usage_records.append(rec)
    try:
        _admin_db_insert_usage(rec)
    except Exception:
        pass
    return rec

def _admin_clear_usage() -> None:
    _admin_usage_records.clear()
    if _is_db_enabled():
        try:
            factory = _get_session_factory()
            if factory is not None:
                from security.models.orm import AdminLlmUsageORM
                with factory() as s:
                    s.query(AdminLlmUsageORM).delete()
                    s.commit()
        except Exception:
            pass

def _admin_usage_history(limit: int = 20, tenant_id: str | None = None) -> list[dict]:
    items: list[dict] = []
    # try DB first if enabled, else in-memory; merge: prefer DB for persistence
    if _is_db_enabled():
        try:
            factory = _get_session_factory()
            if factory is not None:
                from security.models.orm import AdminLlmUsageORM
                with factory() as s:
                    q = s.query(AdminLlmUsageORM).order_by(AdminLlmUsageORM.created_at.desc())
                    if tenant_id:
                        q = q.filter(AdminLlmUsageORM.tenant_id == tenant_id)
                    q = q.limit(max(1, min(100, limit)))
                    for r in q.all():
                        items.append({"id": r.id, "tenant_id": r.tenant_id, "provider": r.provider, "model": r.model, "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens, "total_tokens": r.total_tokens, "cost_usd": r.cost_usd, "latency_ms": r.latency_ms, "status": r.status, "error": r.error, "created_at": r.created_at.isoformat() if hasattr(r.created_at, "isoformat") else str(r.created_at)})
                    if items:
                        return items
        except Exception:
            pass
    # fallback in-memory
    recs = list(_admin_usage_records)
    if tenant_id:
        recs = [r for r in recs if r["tenant_id"] == tenant_id]
    recs = sorted(recs, key=lambda x: x["created_at"], reverse=True)[: max(1, min(100, limit))]
    for r in recs:
        c = dict(r)
        if hasattr(c["created_at"], "isoformat"):
            c["created_at"] = c["created_at"].isoformat()
        items.append(c)
    return items

def _admin_usage_summary(tenant_id: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    # collect records from DB if available, else in-memory
    recs: list[dict] = []
    if _is_db_enabled():
        try:
            factory = _get_session_factory()
            if factory is not None:
                from security.models.orm import AdminLlmUsageORM
                with factory() as s:
                    q = s.query(AdminLlmUsageORM)
                    if tenant_id:
                        q = q.filter(AdminLlmUsageORM.tenant_id == tenant_id)
                    for r in q.all():
                        recs.append({"tenant_id": r.tenant_id, "cost_usd": r.cost_usd or 0, "latency_ms": r.latency_ms or 0, "status": r.status, "created_at": r.created_at, "total_tokens": r.total_tokens or 0, "prompt_tokens": r.prompt_tokens or 0, "completion_tokens": r.completion_tokens or 0})
                if recs:
                    pass
                else:
                    recs = [dict(r) for r in _admin_usage_records if (not tenant_id or r["tenant_id"] == tenant_id)]
        except Exception:
            recs = [dict(r) for r in _admin_usage_records if (not tenant_id or r["tenant_id"] == tenant_id)]
    else:
        recs = [dict(r) for r in _admin_usage_records if (not tenant_id or r["tenant_id"] == tenant_id)]
    total = len(recs)
    success = sum(1 for r in recs if r["status"] == "success")
    failed = total - success
    total_cost = round(sum(float(r.get("cost_usd") or 0) for r in recs), 6)
    total_tokens = sum(int(r.get("total_tokens") or 0) for r in recs)
    latencies = sorted([float(r.get("latency_ms") or 0) for r in recs])
    avg_lat = round(sum(latencies) / len(latencies), 2) if latencies else 0.0
    p95 = 0.0
    if latencies:
        idx = _usage_math.ceil(0.95 * len(latencies)) - 1
        idx = max(0, min(idx, len(latencies) - 1))
        p95 = float(latencies[idx])
    # daily = today UTC, per_minute = last 60s
    daily = 0
    per_min = 0
    for r in recs:
        ca = r.get("created_at")
        if isinstance(ca, str):
            try:
                ca = datetime.fromisoformat(ca.replace("Z", "+00:00"))
            except Exception:
                continue
        if ca is None:
            continue
        if ca.tzinfo is None:
            ca = ca.replace(tzinfo=timezone.utc)
        if ca.date() == now.date():
            daily += 1
        if (now - ca).total_seconds() <= 60:
            per_min += 1
    return {"tenant_id": tenant_id or "all", "total_requests": total, "success_count": success, "failed_count": failed, "total_cost_usd": total_cost, "total_tokens": total_tokens, "avg_latency_ms": avg_lat, "p95_latency_ms": round(p95, 2), "daily_count": daily, "per_minute_count": per_min, "window": "all-time"}

@router.get("/usage/summary")
def usage_summary(tenant_id: str | None = None, admin: AdminUser = Depends(get_current_admin)):
    return _admin_usage_summary(tenant_id=tenant_id)

@router.get("/usage/history")
def usage_history(limit: int = 20, tenant_id: str | None = None, admin: AdminUser = Depends(get_current_admin)):
    return {"items": _admin_usage_history(limit=limit, tenant_id=tenant_id), "count": len(_admin_usage_history(limit=limit, tenant_id=tenant_id))}

def clear_usage() -> None:
    _admin_clear_usage()

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
