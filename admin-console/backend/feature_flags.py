"""Feature flags console surface (admin-console/backend/feature_flags.py).

GET  /v1/feature-flags — list flags with effective state (auth)
PUT  /v1/feature-flags — toggle a flag (L5, admin_settings.feature_flags JSON)

Storage: NEW admin_settings key 'feature_flags' holding a JSON object
{flag_name: bool} of console-stored overrides. GET merges the built-in
catalog defaults with stored overrides (unknown stored names are shown as
custom flags).

HARD RULE (P2, additive-only): flags are console-stored only — no runtime
consumer reads this key yet, so toggling NEVER changes runtime behavior.
The API marks every response accordingly (runtime_wired=false).
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
router = APIRouter(prefix="/v1/feature-flags", tags=["feature-flags"])

FLAGS_KEY = "feature_flags"

# Built-in catalog: name -> (default, description). Console-only semantics.
DEFAULT_FLAGS: dict[str, tuple[bool, str]] = {
    "maintenance_banner": (False, "Show a maintenance banner in the admin console"),
    "beta_llm_dashboard": (False, "Enable beta LLM dashboard widgets (console display only)"),
    "audit_export_csv": (True, "Enable the audit CSV export button (console display only)"),
    "quota_override_planned": (False, "Display quota overrides as planned (no enforcement effect)"),
    "embedding_console_edit": (True, "Allow editing embedding config in the console (restart required to apply)"),
    "outline_sync_v2": (False, "Use the Outline sync v2 UI flow (console only, worker unchanged)"),
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

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
        logger.debug(f"flags DB engine failed: {e}")
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
            row = conn.execute(text("SELECT value FROM admin_settings WHERE key='feature_flags'")).fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logger.debug(f"flags DB read failed: {e}")
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
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES ('feature_flags', :v, :now, :by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception:
                pass
            try:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES ('feature_flags', :v, :now, :by)"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception as e2:
                logger.debug(f"flags DB write fallback failed: {e2}")
                return False
    except Exception as e:
        logger.debug(f"flags DB write failed: {e}")
        return False


def _load_overrides() -> tuple[dict, str]:
    global _inmem
    raw = _db_get_raw()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                clean = {str(k): bool(v) for k, v in data.items() if _NAME_RE.match(str(k))}
                _inmem = clean
                return clean, "db"
        except Exception as e:
            logger.debug(f"flags parse DB failed: {e}")
    if _inmem is not None:
        return dict(_inmem), "in-memory"
    return {}, "defaults"


def _merged_view(overrides: dict) -> list[dict]:
    names = list(DEFAULT_FLAGS.keys()) + [n for n in overrides.keys() if n not in DEFAULT_FLAGS]
    out: list[dict] = []
    for name in names:
        if name in DEFAULT_FLAGS:
            default, desc = DEFAULT_FLAGS[name]
            custom = False
        else:
            default, desc, custom = False, "", True
        enabled = bool(overrides[name]) if name in overrides else default
        out.append({"name": name, "enabled": enabled, "default": default,
                    "overridden": name in overrides, "custom": custom,
                    "description": desc})
    return out


class FlagUpdateRequest(BaseModel):
    name: str = Field(max_length=64)
    enabled: bool

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not _NAME_RE.match(v):
            raise ValueError("name must match ^[a-z][a-z0-9_]{1,63}$")
        return v


_CONSOLE_ONLY_NOTE = ("Console-stored only (runtime_wired=false): "
                      "toggling never changes runtime behavior.")


@router.get("")
@router.get("/")
def flags_list(admin: AdminUser = Depends(get_current_admin)) -> dict:
    overrides, source = _load_overrides()
    return {"count": len(DEFAULT_FLAGS) + sum(1 for n in overrides if n not in DEFAULT_FLAGS),
            "flags": _merged_view(overrides), "source": source,
            "runtime_wired": False, "note": _CONSOLE_ONLY_NOTE}


@router.put("")
@router.put("/")
def flags_toggle(req: FlagUpdateRequest,
                 admin: AdminUser = Depends(require_l5)) -> dict:
    global _inmem
    overrides, _ = _load_overrides()
    overrides[req.name] = bool(req.enabled)
    raw = json.dumps(overrides)
    ok = _db_set_raw(raw, updated_by=getattr(admin, "email", None))
    default = DEFAULT_FLAGS[req.name][0] if req.name in DEFAULT_FLAGS else False
    entry = {"name": req.name, "enabled": bool(req.enabled), "default": default,
             "overridden": True, "custom": req.name not in DEFAULT_FLAGS,
             "description": DEFAULT_FLAGS[req.name][1] if req.name in DEFAULT_FLAGS else ""}
    if ok:
        _inmem = dict(overrides)
        return {**entry, "source": "db", "runtime_wired": False, "note": _CONSOLE_ONLY_NOTE}
    if os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod"):
        raise HTTPException(status_code=503, detail="Feature flags DB unavailable in production (fail-closed)")
    _inmem = dict(overrides)
    return {**entry, "source": "in-memory", "runtime_wired": False,
            "note": _CONSOLE_ONLY_NOTE + " Saved in-memory only (dev)."}
