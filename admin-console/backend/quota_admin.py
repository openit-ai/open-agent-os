"""Tenant quota admin surface — display/planned overrides only (admin-console/backend/quota_admin.py).

GET  /v1/quota/limits?tenant_id=... — effective limits for a tenant (auth)
PUT  /v1/quota/limits               — L5 update (admin_settings.quota_overrides JSON)
GET  /v1/quota/usage?tenant_id=...  — usage lookup for a tenant (auth, read-only)

HARD RULES (P2, additive-only):
- Enforcement defaults (daily 100 / per-minute 10) live in
  agent_runtime.llm_runtime and are NEVER imported, read, or changed here.
  The constants below are display-only mirrors used for the GET display API.
- PUT writes ONLY to the NEW admin_settings key 'quota_overrides'
  (JSON object keyed by tenant_id). The runtime enforcement path is untouched;
  overrides are surfaced via the GET display API and marked console/planned
  until runtime wiring lands.
- Usage lookup is read-only: best-effort reuse of the existing
  llm_providers usage summary; quota state is never mutated here.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

try:
    from .auth import AdminUser, get_current_admin, require_l5
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/quota", tags=["quota"])

QUOTA_OVERRIDES_KEY = "quota_overrides"

# Display-only mirrors of the runtime enforcement defaults.
# DO NOT change: enforcement lives in agent_runtime.llm_runtime (100/10).
DEFAULT_DAILY_LIMIT = 100
DEFAULT_PER_MINUTE_LIMIT = 10

MAX_DAILY_LIMIT = 1_000_000
MAX_PER_MINUTE_LIMIT = 100_000

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
        logger.debug(f"quota DB engine failed: {e}")
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
            row = conn.execute(text("SELECT value FROM admin_settings WHERE key='quota_overrides'")).fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logger.debug(f"quota DB read failed: {e}")
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
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES ('quota_overrides', :v, :now, :by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception:
                pass
            try:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES ('quota_overrides', :v, :now, :by)"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception as e2:
                logger.debug(f"quota DB write fallback failed: {e2}")
                return False
    except Exception as e:
        logger.debug(f"quota DB write failed: {e}")
        return False


def _norm_tenant(v: str | None) -> str:
    t = (v or "default").strip() or "default"
    if len(t) > 128:
        raise HTTPException(status_code=422, detail="tenant_id must be <= 128 chars")
    if not re.match(r"^[A-Za-z0-9_.\-:]+$", t):
        raise HTTPException(status_code=422, detail="tenant_id contains invalid characters")
    return t


def _load_overrides() -> tuple[dict, str]:
    """Return (overrides dict keyed by tenant, source)."""
    global _inmem
    raw = _db_get_raw()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                _inmem = data
                return data, "db"
        except Exception as e:
            logger.debug(f"quota parse DB failed: {e}")
    if _inmem is not None:
        return dict(_inmem), "in-memory"
    return {}, "defaults"


def _effective_for(overrides: dict, tenant_id: str) -> tuple[dict, dict | None]:
    entry = overrides.get(tenant_id)
    if isinstance(entry, dict):
        try:
            daily = int(entry.get("daily_limit", DEFAULT_DAILY_LIMIT))
        except Exception:
            daily = DEFAULT_DAILY_LIMIT
        try:
            per_min = int(entry.get("per_minute_limit", DEFAULT_PER_MINUTE_LIMIT))
        except Exception:
            per_min = DEFAULT_PER_MINUTE_LIMIT
        if not (1 <= daily <= MAX_DAILY_LIMIT):
            daily = DEFAULT_DAILY_LIMIT
        if not (1 <= per_min <= MAX_PER_MINUTE_LIMIT):
            per_min = DEFAULT_PER_MINUTE_LIMIT
        eff = {"daily_limit": daily, "per_minute_limit": per_min}
        return eff, {"daily_limit": daily, "per_minute_limit": per_min,
                     "updated_at": entry.get("updated_at"), "updated_by": entry.get("updated_by")}
    return {"daily_limit": DEFAULT_DAILY_LIMIT, "per_minute_limit": DEFAULT_PER_MINUTE_LIMIT}, None


class QuotaUpdateRequest(BaseModel):
    tenant_id: str = Field(default="default", max_length=128)
    daily_limit: Optional[int] = Field(default=None, ge=1, le=MAX_DAILY_LIMIT)
    per_minute_limit: Optional[int] = Field(default=None, ge=1, le=MAX_PER_MINUTE_LIMIT)

    @field_validator("tenant_id")
    @classmethod
    def check_tenant(cls, v: str) -> str:
        v = (v or "default").strip() or "default"
        if not re.match(r"^[A-Za-z0-9_.\-:]+$", v):
            raise ValueError("tenant_id contains invalid characters")
        return v


_ENFORCEMENT_NOTE = ("Runtime enforcement unchanged (100/day, 10/min in llm_runtime). "
                      "Overrides are console display / planned only.")


@router.get("/limits")
def quota_get_limits(tenant_id: str = "default",
                     admin: AdminUser = Depends(get_current_admin)) -> dict:
    tid = _norm_tenant(tenant_id)
    overrides, source = _load_overrides()
    eff, entry = _effective_for(overrides, tid)
    return {
        "tenant_id": tid,
        "daily_limit": eff["daily_limit"],
        "per_minute_limit": eff["per_minute_limit"],
        "defaults": {"daily_limit": DEFAULT_DAILY_LIMIT,
                     "per_minute_limit": DEFAULT_PER_MINUTE_LIMIT},
        "override": entry,
        "overridden": entry is not None,
        "source": ("db-override" if (source == "db" and entry is not None)
                   else ("db" if source == "db" else source)),
        "enforcement": "unchanged",
        "note": _ENFORCEMENT_NOTE,
    }


@router.put("/limits")
def quota_put_limits(req: QuotaUpdateRequest,
                     admin: AdminUser = Depends(require_l5)) -> dict:
    global _inmem
    if req.daily_limit is None and req.per_minute_limit is None:
        raise HTTPException(status_code=422, detail="at least one of daily_limit / per_minute_limit is required")
    tid = _norm_tenant(req.tenant_id)
    overrides, _ = _load_overrides()
    prev = overrides.get(tid) if isinstance(overrides.get(tid), dict) else {}
    entry = {
        "daily_limit": req.daily_limit if req.daily_limit is not None else int(prev.get("daily_limit", DEFAULT_DAILY_LIMIT)),
        "per_minute_limit": req.per_minute_limit if req.per_minute_limit is not None else int(prev.get("per_minute_limit", DEFAULT_PER_MINUTE_LIMIT)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": getattr(admin, "email", None),
    }
    overrides[tid] = entry
    raw = json.dumps(overrides)
    ok = _db_set_raw(raw, updated_by=getattr(admin, "email", None))
    if ok:
        _inmem = dict(overrides)
        return {"tenant_id": tid, **entry, "overridden": True, "source": "db",
                "enforcement": "unchanged", "note": _ENFORCEMENT_NOTE}
    if os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod"):
        raise HTTPException(status_code=503, detail="Quota overrides DB unavailable in production (fail-closed)")
    _inmem = dict(overrides)
    return {"tenant_id": tid, **entry, "overridden": True, "source": "in-memory",
            "enforcement": "unchanged",
            "note": _ENFORCEMENT_NOTE + " Saved in-memory only (dev)."}


def _usage_summary_best_effort(tenant_id: str) -> tuple[dict | None, str]:
    """Read-only reuse of the existing llm_providers usage summary. Never mutates."""
    try:
        import sys as _sys
        mod = _sys.modules.get("admin_console.backend.llm_providers")
        if mod is None:
            try:
                import llm_providers as mod  # type: ignore
            except Exception:
                mod = None
        if mod is not None and hasattr(mod, "_admin_usage_summary"):
            summary = mod._admin_usage_summary(tenant_id=tenant_id)  # type: ignore
            if summary is not None and isinstance(summary, dict):
                return dict(summary), "llm_providers"
    except Exception as e:
        logger.debug(f"quota usage summary failed: {e}")
    return None, "unavailable"


@router.get("/usage")
def quota_get_usage(tenant_id: str = "default",
                    admin: AdminUser = Depends(get_current_admin)) -> dict:
    tid = _norm_tenant(tenant_id)
    overrides, _ = _load_overrides()
    eff, _ = _effective_for(overrides, tid)
    summary, usrc = _usage_summary_best_effort(tid)
    return {
        "tenant_id": tid,
        "effective_limits": eff,
        "usage": summary,
        "usage_source": usrc,
        "enforcement": "unchanged",
        "note": ("Read-only usage lookup. " + _ENFORCEMENT_NOTE) if summary is not None
                else "Usage store unavailable (no records yet). " + _ENFORCEMENT_NOTE,
    }
