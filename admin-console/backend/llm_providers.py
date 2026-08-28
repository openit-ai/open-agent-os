"""Admin Console — LLM Providers (frontend settings).

Routes:
- GET  /v1/llm/providers            — list (any authenticated admin)
- POST /v1/llm/providers            — create (L5 only)
- GET  /v1/llm/providers/{id}       — get single
- PATCH /v1/llm/providers/{id}      — update (L5)
- DELETE /v1/llm/providers/{id}     — delete (L5)
- POST /v1/llm/providers/{id}/test  — test connection (L5, mock)
- PATCH /v1/llm/providers/{id}/toggle — toggle enabled (L5) alternative POST /toggle

Provider types: claude, codex, gemini, opencode, ollama
Field mapping:
  claude/codex/gemini -> apiKey (+ baseUrl optional, model)
  opencode            -> path (+ model optional)
  ollama              -> url (+ model)

In-memory store with optional DB stub (fallback). API keys are stored raw in memory but returned masked.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

try:
    from .auth import AdminUser, get_current_admin, require_l5
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

router = APIRouter(prefix="/v1/llm", tags=["llm"])


class ProviderType(str, Enum):
    claude = "claude"
    codex = "codex"
    gemini = "gemini"
    opencode = "opencode"
    ollama = "ollama"


# Types that require apiKey
_APIKEY_TYPES = {ProviderType.claude, ProviderType.codex, ProviderType.gemini}


class LLMProvider(BaseModel):
    id: str
    provider: ProviderType
    name: str = ""
    api_key: Optional[str] = None  # masked on output
    api_key_masked: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    path: Optional[str] = None  # for opencode
    url: Optional[str] = None  # for ollama
    enabled: bool = True
    created_at: datetime
    updated_at: datetime
    last_test_at: Optional[datetime] = None
    last_test_status: Optional[str] = None  # ok / failed
    last_test_latency_ms: Optional[float] = None


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


# In-memory store
_providers: dict[str, LLMProvider] = {}


def clear_providers() -> None:
    _providers.clear()


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
    # For create, enforce required; for update, only if provider changed or explicit
    if provider in _APIKEY_TYPES:
        # apiKey required unless update and no key provided (keep existing)
        if not is_update and not data.get("api_key"):
            raise HTTPException(status_code=400, detail=f"apiKey is required for provider '{provider.value}'")
    elif provider == ProviderType.opencode:
        if not is_update and not data.get("path"):
            raise HTTPException(status_code=400, detail="path is required for provider 'opencode'")
    elif provider == ProviderType.ollama:
        if not is_update and not data.get("url"):
            raise HTTPException(status_code=400, detail="url is required for provider 'ollama'")


def _to_public(p: LLMProvider) -> dict:
    d = p.model_dump(mode="json")
    # mask api_key for public output, keep both keys for compat
    raw = d.get("api_key")
    masked = _mask_key(raw)
    d["api_key_masked"] = masked
    # also expose camelCase aliases for frontend
    d["apiKey"] = masked  # frontend sees masked
    d["baseUrl"] = d.get("base_url")
    # never leak raw key via list; but allow raw in create response masked already
    # keep raw hidden: replace with masked? keep masked only
    # frontend will display masked; edit will re-send if changed
    d["api_key"] = masked
    return d


@router.get("/providers")
def list_providers(admin: AdminUser = Depends(get_current_admin)):
    items = [_to_public(v) for v in sorted(_providers.values(), key=lambda x: x.created_at)]
    return {"providers": items, "items": items, "count": len(items), "total": len(items)}


@router.post("/providers", status_code=201)
def create_provider(payload: LLMProviderCreate, admin: AdminUser = Depends(require_l5)):
    data = _normalize_create(payload)
    _validate_fields(data["provider"], data, is_update=False)
    pid = f"llm_{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    provider = LLMProvider(
        id=pid,
        provider=data["provider"],
        name=data["name"],
        api_key=data["api_key"],
        base_url=data["base_url"],
        model=data["model"],
        path=data["path"],
        url=data["url"],
        enabled=data["enabled"],
        created_at=now,
        updated_at=now,
    )
    provider.api_key_masked = _mask_key(provider.api_key)
    _providers[pid] = provider
    return _to_public(provider)


@router.get("/providers/{provider_id}")
def get_provider(provider_id: str, admin: AdminUser = Depends(get_current_admin)):
    p = _providers.get(provider_id)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")
    return _to_public(p)


@router.patch("/providers/{provider_id}")
def update_provider(provider_id: str, payload: LLMProviderUpdate, admin: AdminUser = Depends(require_l5)):
    p = _providers.get(provider_id)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")
    # resolve new provider type
    new_provider = payload.provider if payload.provider is not None else p.provider
    # collect updates
    api_key = payload.apiKey if payload.apiKey is not None else payload.api_key
    base_url = payload.baseUrl if payload.baseUrl is not None else payload.base_url
    updates: dict = {}
    if payload.provider is not None:
        updates["provider"] = payload.provider
    if payload.name is not None:
        updates["name"] = payload.name
    if api_key is not None:
        # if masked placeholder sent, ignore
        if api_key and "***" in api_key:
            pass
        else:
            updates["api_key"] = api_key
    if base_url is not None:
        updates["base_url"] = base_url
    if payload.model is not None:
        updates["model"] = payload.model
    if payload.path is not None:
        updates["path"] = payload.path
    if payload.url is not None:
        updates["url"] = payload.url
    if payload.enabled is not None:
        updates["enabled"] = bool(payload.enabled)

    # validate if provider changed
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
    p.api_key_masked = _mask_key(p.api_key)
    _providers[provider_id] = p
    return _to_public(p)


@router.delete("/providers/{provider_id}")
def delete_provider(provider_id: str, admin: AdminUser = Depends(require_l5)):
    if provider_id not in _providers:
        raise HTTPException(status_code=404, detail="provider not found")
    del _providers[provider_id]
    return {"status": "deleted", "id": provider_id}


@router.post("/providers/{provider_id}/test")
def test_provider(provider_id: str, admin: AdminUser = Depends(require_l5)):
    p = _providers.get(provider_id)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")
    # Mock test: simulate latency, check required field present
    start = time.perf_counter()
    # tiny delay
    time.sleep(0.05)
    latency = round((time.perf_counter() - start) * 1000, 1)
    # validate
    ok = True
    reason = "ok"
    if p.provider in _APIKEY_TYPES and not p.api_key:
        ok = False
        reason = "missing apiKey"
    elif p.provider == ProviderType.opencode and not p.path:
        ok = False
        reason = "missing path"
    elif p.provider == ProviderType.ollama and not p.url:
        ok = False
        reason = "missing url"
    p.last_test_at = datetime.now(timezone.utc)
    p.last_test_status = "ok" if ok else "failed"
    p.last_test_latency_ms = latency
    _providers[provider_id] = p
    return {"status": "ok" if ok else "failed", "latency_ms": latency, "detail": reason, "provider_id": provider_id}


@router.post("/providers/{provider_id}/toggle")
@router.patch("/providers/{provider_id}/toggle")
def toggle_provider(provider_id: str, admin: AdminUser = Depends(require_l5)):
    p = _providers.get(provider_id)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")
    p.enabled = not p.enabled
    p.updated_at = datetime.now(timezone.utc)
    _providers[provider_id] = p
    return _to_public(p)
