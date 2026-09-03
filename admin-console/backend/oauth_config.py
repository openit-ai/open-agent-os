"""OAuth config API — Google/Microsoft login connectors (admin-console/backend/oauth_config.py).

GET  /v1/oauth/config — read effective config (auth; secrets only as set-flags)
PUT  /v1/oauth/config — L5 update of NON-SECRET prefs only (admin_settings.oauth_config JSON)
POST /v1/oauth/test   — L5 config-presence check + IdP discovery-doc reachability probe

Secrets (client-id/secret) are NEVER stored in the DB by this module. They must
be provided via host env (GOOGLE_CLIENT_ID/SECRET, MS_CLIENT_ID/SECRET or
MICROSOFT_CLIENT_ID/SECRET). PUT rejects any secret field with 400 and stores
only non-secret prefs (provider enabled flags, display names). Redirect URIs
shown here are derived from *_REDIRECT_URI env or the documented default
pattern. Independent of MM/Outline modules (own helpers, own admin_settings key).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

try:
    from .auth import AdminUser, get_current_admin, require_l5
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/oauth", tags=["oauth"])

OAUTH_KEY = "oauth_config"

GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
MS_DISCOVERY = "https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration"

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
        logger.debug(f"oauth DB engine failed: {e}")
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
            row = conn.execute(text("SELECT value FROM admin_settings WHERE key='oauth_config'")).fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logger.debug(f"oauth DB read failed: {e}")
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
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES ('oauth_config', :v, :now, :by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception:
                pass
            try:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES ('oauth_config', :v, :now, :by)"),
                             {"v": value_json, "now": now, "by": updated_by})
                return True
            except Exception as e2:
                logger.debug(f"oauth DB write fallback failed: {e2}")
                return False
    except Exception as e:
        logger.debug(f"oauth DB write failed: {e}")
        return False


def _redirect_default(provider: str) -> str:
    base = (os.environ.get("OAOS_PUBLIC_URL") or os.environ.get("ADMIN_PUBLIC_URL") or "").strip().rstrip("/")
    if base:
        return f"{base}/api/auth/callback/{provider}"
    return f"https://<admin-host>/api/auth/callback/{provider}"


def _env_config() -> dict:
    return {
        "google_client_id_set": bool((os.environ.get("GOOGLE_CLIENT_ID") or "").strip()),
        "google_client_secret_set": bool((os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()),
        "google_redirect_uri": (os.environ.get("GOOGLE_REDIRECT_URI") or "").strip() or _redirect_default("google"),
        "microsoft_client_id_set": bool((os.environ.get("MICROSOFT_CLIENT_ID") or os.environ.get("MS_CLIENT_ID") or "").strip()),
        "microsoft_client_secret_set": bool((os.environ.get("MICROSOFT_CLIENT_SECRET") or os.environ.get("MS_CLIENT_SECRET") or "").strip()),
        "microsoft_redirect_uri": (os.environ.get("MICROSOFT_REDIRECT_URI") or os.environ.get("MS_REDIRECT_URI") or "").strip() or _redirect_default("microsoft"),
    }


def _load_prefs() -> tuple[dict, str]:
    global _inmem
    raw = _db_get_raw()
    if raw:
        try:
            data = json.loads(raw)
            prefs = {
                "google_enabled": bool(data.get("google_enabled", False)),
                "microsoft_enabled": bool(data.get("microsoft_enabled", False)),
            }
            _inmem = prefs
            return prefs, "db"
        except Exception as e:
            logger.debug(f"oauth parse DB failed: {e}")
    if _inmem is not None:
        return dict(_inmem), "in-memory"
    return {"google_enabled": False, "microsoft_enabled": False}, "env"


class OAuthUpdateRequest(BaseModel):
    google_enabled: Optional[bool] = Field(default=None)
    microsoft_enabled: Optional[bool] = Field(default=None)

    model_config = {"extra": "forbid"}


@router.get("/config")
def oauth_get_config(admin: AdminUser = Depends(get_current_admin)) -> dict:
    prefs, source = _load_prefs()
    return {**_env_config(), **prefs, "source": source, "applied": True,
            "note": "client-id/secret are env-only (GOOGLE_* / MS_* on the host); DB stores display prefs only"}


@router.put("/config")
def oauth_put_config(req: OAuthUpdateRequest, admin: AdminUser = Depends(require_l5)) -> dict:
    global _inmem
    prefs, _ = _load_prefs()
    if req.google_enabled is not None:
        prefs["google_enabled"] = req.google_enabled
    if req.microsoft_enabled is not None:
        prefs["microsoft_enabled"] = req.microsoft_enabled
    raw = json.dumps(prefs)
    ok = _db_set_raw(raw, updated_by=getattr(admin, "email", None))
    if ok:
        _inmem = dict(prefs)
        return {**_env_config(), **prefs, "source": "db", "applied": True,
                "note": "prefs saved; client-id/secret must be set via host env (never stored here)"}
    if (os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod")):
        raise HTTPException(status_code=503, detail="OAuth prefs DB unavailable in production (fail-closed)")
    _inmem = dict(prefs)
    return {**_env_config(), **prefs, "source": "in-memory", "applied": True,
            "note": "prefs saved in-memory only (dev); client-id/secret must be set via host env"}


@router.post("/test")
def oauth_test(body: dict | None = None, admin: AdminUser = Depends(require_l5)) -> dict:
    """Presence check for env credentials + IdP discovery-doc reachability (no secrets used)."""
    prefs, source = _load_prefs()
    env = _env_config()
    provider = ""
    if isinstance(body, dict):
        provider = str(body.get("provider") or "").strip().lower()
    if provider and provider not in ("google", "microsoft"):
        raise HTTPException(status_code=400, detail="provider must be google|microsoft")
    targets = {"google": GOOGLE_DISCOVERY, "microsoft": MS_DISCOVERY}
    names = [provider] if provider else ["google", "microsoft"]
    results: dict = {}
    overall = True
    for name in names:
        id_set = env[f"{name}_client_id_set"]
        secret_set = env[f"{name}_client_secret_set"]
        if not (id_set and secret_set):
            results[name] = {"ok": False, "configured": False,
                             "error": f"{name} credentials missing: set {name.upper()}_CLIENT_ID/SECRET env on the host"}
            overall = False
            continue
        t0 = time.monotonic()
        try:
            import httpx
            r = httpx.get(targets[name], timeout=8.0)
            ms = round((time.monotonic() - t0) * 1000, 1)
            if r.status_code == 200:
                results[name] = {"ok": True, "configured": True, "status_code": 200,
                                 "latency_ms": ms, "enabled": prefs.get(f"{name}_enabled", False)}
            else:
                results[name] = {"ok": False, "configured": True, "status_code": r.status_code,
                                 "error": r.text[:200], "latency_ms": ms}
                overall = False
        except Exception as e:
            results[name] = {"ok": False, "configured": True,
                             "error": f"{type(e).__name__}: {str(e)[:160]}"}
            overall = False
    return {"ok": overall, "providers": results, "redirect_uris": {
        "google": env["google_redirect_uri"], "microsoft": env["microsoft_redirect_uri"]}, "source": source}
