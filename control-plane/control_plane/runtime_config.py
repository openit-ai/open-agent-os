"""Control Plane Runtime Configuration Plane — Stage-1 minimal slice.

Reads canonical signed snapshot published by Admin API (via same DB admin_settings
or HTTP fallback), verifies HMAC signature (fail-closed in production), and
exposes applied state (version, applied_by/at, process identity).

No network fan-out beyond DB/optional Admin HTTP; no external Mattermost/git/restart.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
import logging
logger = logging.getLogger(__name__)

try:
    from sqlalchemy.exc import SQLAlchemyError
except (ImportError, ModuleNotFoundError):  # sqlalchemy is lazy/optional; best-effort fallback
    SQLAlchemyError = Exception  # type: ignore


class RuntimeConfigValidationError(ValueError):
    """The durable runtime-config payload is malformed or not serializable."""


class RuntimeConfigBackendError(RuntimeError):
    """The configured runtime-config backend cannot serve the request."""

router = APIRouter(prefix="/v1/runtime-config", tags=["runtime-config"])
internal_router = APIRouter(prefix="/v1/internal/runtime-config", tags=["runtime-config-internal"])

_DEV_SIGNING_KEY = "dev-runtime-config-signing-key-change-in-prod-32b"

def _is_production() -> bool:
    for k in ("OAOS_ENV","ENV","OAOS_ENVIRONMENT","APP_ENV","ENVIRONMENT"):
        if os.getenv(k,"").strip().lower() in ("production","prod"):
            return True
    return False

def _get_signing_key() -> str:
    for k in ("OAOS_RUNTIME_CONFIG_SIGNING_KEY","OAOS_RC_SIGNING_KEY","OAOS_SIGNING_KEY","ADMIN_JWT_SECRET"):
        v=os.environ.get(k,"")
        if v and v.strip():
            if _is_production() and v.strip()==_DEV_SIGNING_KEY:
                raise RuntimeError("OAOS_RUNTIME_CONFIG_SIGNING_KEY must be set in production (fail-closed)")
            return v.strip()
    if _is_production():
        raise RuntimeError("OAOS_RUNTIME_CONFIG_SIGNING_KEY/ADMIN_JWT_SECRET required in production (fail-closed)")
    return _DEV_SIGNING_KEY

def _canonical_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode("utf-8")

def _config_hash(config: dict) -> str:
    try:
        return hashlib.sha256(_canonical_bytes(config)).hexdigest()
    except (OverflowError, TypeError, ValueError) as exc:
        raise RuntimeConfigValidationError("runtime config is not JSON serializable") from exc

def _ensure_runtime_tables_sync(engine) -> None:
    try:
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("""CREATE TABLE IF NOT EXISTS admin_runtime_config_snapshots (
                tenant_id TEXT NOT NULL, version INTEGER NOT NULL, snapshot_json TEXT NOT NULL,
                signature TEXT NOT NULL, config_hash TEXT NOT NULL, created_by TEXT NOT NULL,
                created_at TEXT NOT NULL, parent_version INTEGER, rollback_from INTEGER, extra TEXT,
                PRIMARY KEY (tenant_id, version))""" ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_rc_snapshots_tenant_created ON admin_runtime_config_snapshots (tenant_id, created_at)" ))
            conn.execute(text("""CREATE TABLE IF NOT EXISTS admin_runtime_config_published (
                tenant_id TEXT PRIMARY KEY, published_version INTEGER NOT NULL, config_hash TEXT,
                updated_at TEXT NOT NULL, updated_by TEXT NOT NULL, extra TEXT)""" ))
            conn.execute(text("""CREATE TABLE IF NOT EXISTS admin_runtime_config_applied (
                tenant_id TEXT PRIMARY KEY, applied_version INTEGER, config_hash TEXT, applied_at TEXT,
                applied_by TEXT, process_identity TEXT, error TEXT, updated_at TEXT NOT NULL)""" ))
    except (SQLAlchemyError, OSError, ImportError, ModuleNotFoundError) as exc:
        logger.warning(f"runtime-config schema unavailable: {exc}")
        raise RuntimeConfigBackendError("runtime-config schema unavailable") from exc

def _verify_snapshot(snapshot: dict, key: str | None = None) -> bool:
    sig = snapshot.get("signature","")
    if not sig:
        return False
    # Stage-2 includes config_hash in signed payload; keep backward compat with legacy without it
    payload = {k: v for k, v in snapshot.items() if k not in ("signature","published","published_at","published_by","rollback_from","config_hash")}
    # try with config_hash included if present in original signing
    alt_payload = {k: v for k, v in snapshot.items() if k not in ("signature","published","published_at","published_by","rollback_from")}
    try:
        k = key or _get_signing_key()
        expected = hmac.new(k.encode("utf-8"), _canonical_bytes(payload), hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, sig):
            return True
        # fallback: try alt_payload (new style with config_hash)
        if payload != alt_payload:
            expected2 = hmac.new(k.encode("utf-8"), _canonical_bytes(alt_payload), hashlib.sha256).hexdigest()
            if hmac.compare_digest(expected2, sig):
                return True
        return False
    except RuntimeError:
        raise
    except (AttributeError, TypeError, ValueError):
        return False

def _db_url() -> str | None:
    for k in ("OAOS_DATABASE_URL","DATABASE_URL"):
        v=os.environ.get(k,"")
        if v and v.strip():
            return v.strip()
    try:
        from admin_console.backend.persistence import get_database_url as _g  # type: ignore
        u=_g()
        if u and u.strip():
            return u.strip()
    except (ImportError, ModuleNotFoundError, AttributeError, TypeError, ValueError, OSError) as exc:
        logger.warning(f"runtime-config persistence URL lookup failed; environment fallback unavailable: {exc}")
    return None

def _normalize_sync_url(url: str) -> str:
    u=url.strip()
    if u.startswith("postgresql+asyncpg://"):
        u=u.replace("postgresql+asyncpg://","postgresql+psycopg://",1)
    elif u.startswith("postgresql://"):
        u=u.replace("postgresql://","postgresql+psycopg://",1)
    if "+aiosqlite" in u:
        u=u.replace("+aiosqlite","")
        u=u.replace("sqlite+://","sqlite://")
    if u.startswith("sqlite+"):
        u=u.replace("sqlite+","sqlite",1)
    return u


def _validate_snapshot_shape(snapshot: object) -> dict:
    if not isinstance(snapshot, dict):
        raise RuntimeConfigValidationError("published runtime config snapshot must be an object")
    config = snapshot.get("config", {})
    if config is not None and not isinstance(config, dict):
        raise RuntimeConfigValidationError("runtime config snapshot config must be an object")
    providers = (config or {}).get("llm_providers", [])
    if not isinstance(providers, list):
        raise RuntimeConfigValidationError("runtime config llm_providers must be a list")
    if any(not isinstance(provider, dict) for provider in providers):
        raise RuntimeConfigValidationError("runtime config provider entries must be objects")
    return snapshot

def _fetch_via_db(tenant_id: str = "default") -> dict | None:
    url=_db_url()
    if not url:
        return None
    eng=None
    try:
        from sqlalchemy import create_engine, text
        sync_url=_normalize_sync_url(url)
        kwargs: dict={}
        if sync_url.startswith("sqlite"):
            kwargs={}
            if ":memory:" in sync_url:
                kwargs["connect_args"]={"check_same_thread": False}
        else:
            kwargs={"pool_pre_ping": True}
        eng=create_engine(sync_url,**kwargs)
        _ensure_runtime_tables_sync(eng)
        with eng.connect() as conn:
            # 1) try durable published pointer table
            snap=None
            ver=None
            row=conn.execute(text("SELECT published_version, config_hash FROM admin_runtime_config_published WHERE tenant_id=:t"),{"t": tenant_id}).fetchone()
            if row is not None:
                try:
                    ver=int(row[0])
                except (TypeError, ValueError) as exc:
                    raise RuntimeConfigValidationError("published runtime config version is invalid") from exc
            if ver is not None:
                row2=conn.execute(text("SELECT snapshot_json FROM admin_runtime_config_snapshots WHERE tenant_id=:t AND version=:v"),{"t": tenant_id, "v": ver}).fetchone()
                if row2 is not None and row2[0]:
                    try:
                        snap=json.loads(row2[0])
                    except (json.JSONDecodeError, TypeError, ValueError) as exc:
                        raise RuntimeConfigValidationError("published runtime config snapshot JSON is malformed") from exc
            # 2) fallback legacy admin_settings mirror
            if snap is None:
                row=conn.execute(text("SELECT value FROM admin_settings WHERE key=:k"),{"k": f"runtime_config:published:{tenant_id}"}).fetchone()
                if row is None or not row[0]:
                    return None
                try:
                    ver=int(str(row[0]).strip())
                except (TypeError, ValueError) as exc:
                    raise RuntimeConfigValidationError("legacy published runtime config version is invalid") from exc
                row2=conn.execute(text("SELECT value FROM admin_settings WHERE key=:k"),{"k": f"runtime_config:snapshot:{tenant_id}:{ver}"}).fetchone()
                if row2 is None or not row2[0]:
                    return None
                try:
                    snap=json.loads(row2[0])
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    raise RuntimeConfigValidationError("legacy runtime config snapshot JSON is malformed") from exc
            _validate_snapshot_shape(snap)
            # verify
            if not _verify_snapshot(snap):
                if _is_production():
                    raise RuntimeConfigBackendError("published snapshot signature invalid — fail-closed")
                # non-prod: return None to signal invalid
                logger.warning("published runtime config signature invalid; non-production fallback remains unavailable")
                return None
            # ensure config_hash filled
            if snap.get("config_hash") is None and snap.get("config") is not None:
                snap["config_hash"]=_config_hash(snap.get("config",{}))
            # defense: secret raw leak check
            for prov in snap.get("config",{}).get("llm_providers",[]):
                if "encrypted_api_key" in prov or "api_key" in prov:
                    if _is_production():
                        raise RuntimeConfigBackendError("snapshot contains secret raw — fail-closed")
                    logger.warning("snapshot contains secret raw; non-production fallback remains unavailable")
                    return None
            return snap
    except RuntimeConfigValidationError:
        raise
    except RuntimeConfigBackendError as exc:
        logger.warning(f"runtime-config DB backend degraded: {exc}")
        if _is_production():
            raise
        return None
    except (SQLAlchemyError, OSError, ImportError, ModuleNotFoundError) as exc:
        logger.warning(f"runtime-config DB fetch unavailable: {exc}")
        # fail-closed in production on DB outage
        if _is_production():
            raise RuntimeConfigBackendError("runtime-config DB unavailable fail-closed") from exc
        return None
    finally:
        if eng is not None:
            try:
                eng.dispose()
            except SQLAlchemyError:
                logger.debug("cp runtime-config engine dispose failed (best-effort)")

def _fetch_via_admin_module(tenant_id: str="default") -> dict | None:
    # Try in-process import of admin runtime_config module (tests run both in same process)
    import sys
    # admin_api app may have loaded runtime_config; try to find it
    for name in ("admin_console.backend.runtime_config","runtime_config","admin_runtime_config"):
        mod=sys.modules.get(name)
        if mod is not None and hasattr(mod,"get_published_snapshot_internal"):
            try:
                snap=mod.get_published_snapshot_internal(tenant_id)  # type: ignore
            except RuntimeError:
                raise
            except (SQLAlchemyError, OSError, ImportError, ModuleNotFoundError, TypeError, ValueError, AttributeError, KeyError) as exc:
                logger.warning(f"runtime-config admin module unavailable: {exc}")
                snap=None
            if snap is not None:
                _validate_snapshot_shape(snap)
                # verify again under CP key (should match admin key in tests via conftest unified key)
                if not _verify_snapshot(snap):
                    if _is_production():
                        raise RuntimeConfigBackendError("signature invalid fail-closed")
                    logger.warning("admin runtime-config signature invalid; trying durable source")
                    return None
                return snap
    # also try direct admin_settings read via admin module dict
    for name in ("admin_console.backend.runtime_config","runtime_config"):
        mod=sys.modules.get(name)
        if mod is not None and hasattr(mod,"_snapshots"):
            d=getattr(mod,"_snapshots",{})
            pub=getattr(mod,"_published",{})
            ver=pub.get(tenant_id)
            if ver is not None:
                snap=d.get(tenant_id,{}).get(ver)
                if snap is not None:
                    _validate_snapshot_shape(snap)
                    if not _verify_snapshot(snap):
                        if _is_production():
                            raise RuntimeConfigBackendError("signature invalid fail-closed")
                        logger.warning("admin in-memory runtime-config signature invalid")
                        return None
                    return snap
    return None

def fetch_published_snapshot(tenant_id: str="default") -> dict | None:
    """Fetch canonical published snapshot via DB or admin module (in-process).
    Verifies signature; returns None if none published. Raises in prod on invalid sig."""
    # Production never treats the in-process admin module as a durable DB substitute.
    if _is_production():
        return _fetch_via_db(tenant_id)
    # Non-production keeps the explicit in-process fallback used by local/test runs.
    snap=_fetch_via_admin_module(tenant_id)
    if snap is not None:
        return snap
    # 2. try DB
    snap2=_fetch_via_db(tenant_id)
    if snap2 is not None:
        return snap2
    return None

# ── applied state (per-tenant, process-local + durable DB) ─────────────────
_applied: dict[str, dict] = {}  # tenant -> {version, applied_at, applied_by, process_identity}
# hot-reload cache: last applied config per tenant (for safe reference)
_last_applied_config: dict[str, dict] = {}

def _process_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"

def _db_fetch_applied(tenant_id: str) -> dict | None:
    url=_db_url()
    if not url:
        return None
    eng=None
    try:
        from sqlalchemy import create_engine, text
        sync_url=_normalize_sync_url(url)
        kwargs={"connect_args": {"check_same_thread": False}} if "memory" in sync_url else {"pool_pre_ping": True} if sync_url.startswith("sqlite") else {"pool_pre_ping": True}
        # simpler
        if sync_url.startswith("sqlite") and ":memory:" in sync_url:
            kwargs={"connect_args": {"check_same_thread": False}}
        elif sync_url.startswith("sqlite"):
            kwargs={}
        else:
            kwargs={"pool_pre_ping": True}
        eng=create_engine(sync_url,**kwargs)
        _ensure_runtime_tables_sync(eng)
        with eng.connect() as conn:
            row=conn.execute(text("SELECT applied_version, config_hash, applied_at, applied_by, process_identity, error, updated_at FROM admin_runtime_config_applied WHERE tenant_id=:t"),{"t":tenant_id}).fetchone()
            if row is not None:
                return {"tenant_id": tenant_id, "applied_version": row[0], "config_hash": row[1], "applied_at": row[2], "applied_by": row[3], "process_identity": row[4], "error": row[5], "updated_at": row[6]}
    except (SQLAlchemyError, OSError, ImportError, ModuleNotFoundError, ValueError, TypeError, AttributeError) as exc:
        logger.warning(f"runtime-config applied-state read unavailable: {exc}")
        if _is_production():
            raise RuntimeConfigBackendError("applied state read failed fail-closed") from exc
    finally:
        if eng is not None:
            try:
                eng.dispose()
            except SQLAlchemyError:
                logger.debug("cp runtime-config engine dispose failed (best-effort)")
    return None

def _db_upsert_applied(tenant_id: str, applied_version: int, config_hash: str, applied_by: str, process_identity: str, error: str | None) -> None:
    url=_db_url()
    if not url:
        return
    eng=None
    try:
        from sqlalchemy import create_engine, text
        sync_url=_normalize_sync_url(url)
        if sync_url.startswith("sqlite") and ":memory:" in sync_url:
            kwargs={"connect_args": {"check_same_thread": False}}
        elif sync_url.startswith("sqlite"):
            kwargs={}
        else:
            kwargs={"pool_pre_ping": True}
        eng=create_engine(sync_url,**kwargs)
        _ensure_runtime_tables_sync(eng)
        now=datetime.now(timezone.utc).isoformat()
        with eng.begin() as conn:
            try:
                conn.execute(text("INSERT INTO admin_runtime_config_applied (tenant_id, applied_version, config_hash, applied_at, applied_by, process_identity, error, updated_at) VALUES (:t,:v,:ch,:at,:by,:pi,:err,:now) ON CONFLICT (tenant_id) DO UPDATE SET applied_version=:v, config_hash=:ch, applied_at=:at, applied_by=:by, process_identity=:pi, error=:err, updated_at=:now"),
                    {"t":tenant_id,"v":applied_version,"ch":config_hash,"at":now,"by":applied_by,"pi":process_identity,"err":error,"now":now})
            except (SQLAlchemyError, OSError):
                conn.execute(text("INSERT OR REPLACE INTO admin_runtime_config_applied (tenant_id, applied_version, config_hash, applied_at, applied_by, process_identity, error, updated_at) VALUES (:t,:v,:ch,:at,:by,:pi,:err,:now)"),
                    {"t":tenant_id,"v":applied_version,"ch":config_hash,"at":now,"by":applied_by,"pi":process_identity,"err":error,"now":now})
    except (SQLAlchemyError, OSError, ImportError, ModuleNotFoundError, ValueError, TypeError, AttributeError) as exc:
        logger.warning(f"runtime-config applied-state write unavailable: {exc}")
        if _is_production():
            raise RuntimeConfigBackendError("applied state durable write failed fail-closed") from exc
    finally:
        if eng is not None:
            try:
                eng.dispose()
            except SQLAlchemyError:
                logger.debug("cp runtime-config engine dispose failed (best-effort)")

def _apply_hot_reload(tenant_id: str, snapshot: dict) -> tuple[bool, str | None]:
    # Safe hot-reload for supported fields: runtime_mode, user/agent mapping reference, infra reference, provider/fallback metadata
    # Returns (ok, error). Never raises — fail-soft but record error for status.
    try:
        config=snapshot.get("config",{})
        # validate runtime_mode
        rm=config.get("runtime_mode")
        if rm is not None and rm not in ("hermes","llm"):
            return False, f"invalid runtime_mode {rm}"
        # secret raw already verified elsewhere
        for prov in config.get("llm_providers",[]):
            if "encrypted_api_key" in prov or "api_key" in prov:
                return False, "snapshot contains secret raw — rejected"
        # store for reference; actual subsystem reload would be explicit hot-reload hook
        _last_applied_config[tenant_id]=config
        # Example hot-reload: update env for hermes base url if present (safe)
        hermes=config.get("hermes",{})
        if hermes.get("base_url"):
            os.environ["OAOS_CP_HERMES_BASE_URL"]=hermes.get("base_url")
        # fallback chain metadata stored only
        return True, None
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return False, str(exc)[:300]

def mark_applied(tenant_id: str, snapshot: dict, applied_by: str) -> dict:
    ver=snapshot.get("version")
    ch=snapshot.get("config_hash") or _config_hash(snapshot.get("config",{}))
    now=datetime.now(timezone.utc).isoformat()
    pid=_process_identity()
    # hot-reload attempt
    ok, err=_apply_hot_reload(tenant_id, snapshot)
    rec={
        "tenant_id": tenant_id,
        "version": ver,
        "applied_version": ver,
        "applied_at": now,
        "applied_by": applied_by,
        "process_identity": pid,
        "config_hash": ch,
        "signature": snapshot.get("signature","")[:16]+"...",
        "error": err,
        "hot_reload_ok": ok,
    }
    _applied[tenant_id]=rec
    # durable
    _db_upsert_applied(tenant_id, int(ver) if ver is not None else 0, ch, applied_by, pid, err)
    return rec

def get_applied(tenant_id: str="default") -> dict | None:
    # DB primary if available
    db_rec=_db_fetch_applied(tenant_id)
    if db_rec is not None and db_rec.get("applied_version") is not None:
        # merge with in-memory for richer fields
        mem=_applied.get(tenant_id)
        if mem is not None:
            # prefer DB timestamps but keep mem signature etc
            merged={**db_rec, **mem}
            merged["config_hash"]=db_rec.get("config_hash") or mem.get("config_hash")
            return merged
        # construct minimal
        return {"tenant_id": tenant_id, "version": db_rec.get("applied_version"), "applied_version": db_rec.get("applied_version"), "applied_at": db_rec.get("applied_at"), "applied_by": db_rec.get("applied_by"), "process_identity": db_rec.get("process_identity"), "config_hash": db_rec.get("config_hash"), "error": db_rec.get("error")}
    return _applied.get(tenant_id)

def _is_destructive_allowed() -> bool:
    """Strict guard: destructive DB clear only for sqlite isolated URLs, never in production.
    OAOS_ALLOW_DESTRUCTIVE flag does NOT bypass production or non-sqlite checks (defense-in-depth)."""
    if _is_production():
        return False
    url = (_db_url() or "").strip().lower()
    if url.startswith("sqlite"):
        return True
    # explicit flag still requires sqlite (prevents postgres wipe even if flag set)
    if os.environ.get("OAOS_ALLOW_DESTRUCTIVE_RUNTIME_CONFIG_CLEAR") == "1":
        return url.startswith("sqlite")
    if not url:
        return False
    # any postgres/mysql/etc => not allowed
    return False

def clear_runtime_config_state() -> None:
    _applied.clear()
    _last_applied_config.clear()
    # Guard: tests must use isolated sqlite DB; never wipe production postgres
    if not _is_destructive_allowed():
        _url = (_db_url() or "").strip()
        if _is_production():
            logger.debug("clear_runtime_config_state: skipped DB wipe (production guard)")
        elif _url and not _url.lower().startswith("sqlite"):
            logger.debug(f"clear_runtime_config_state: skipped DB wipe for non-sqlite URL { _url[:20]}… (use sqlite for tests)")
        elif not _url:
            logger.debug("clear_runtime_config_state: skipped DB wipe (no URL, in-memory only)")
        else:
            logger.debug("clear_runtime_config_state: skipped DB wipe (guard)")
        return
    # also clear DB durable (best-effort) — only for sqlite isolated tests
    url=_db_url()
    if url:
        eng=None
        try:
            from sqlalchemy import create_engine, text
            sync_url=_normalize_sync_url(url)
            if sync_url.startswith("sqlite") and ":memory:" in sync_url:
                kwargs={"connect_args": {"check_same_thread": False}}
            elif sync_url.startswith("sqlite"):
                kwargs={}
            else:
                kwargs={"pool_pre_ping": True}
            eng=create_engine(sync_url,**kwargs)
            _ensure_runtime_tables_sync(eng)
            with eng.begin() as conn:
                conn.execute(text("DELETE FROM admin_runtime_config_applied WHERE 1=1"))
        except (SQLAlchemyError, OSError, ImportError, ModuleNotFoundError, ValueError, TypeError, AttributeError) as exc:
            # This is isolated test cleanup only; preserve the primary test
            # result while making cleanup degradation visible.
            logger.warning(f"clear runtime-config state cleanup failed: {exc}")
        finally:
            if eng is not None:
                try:
                    eng.dispose()
                except SQLAlchemyError:
                    logger.debug("cp runtime-config engine dispose failed (best-effort)")

# ── auth helper (reuse CP auth; never hide an auth backend failure) ─────────
def _resolve_caller(authorization: str | None, x_user_id: str | None) -> str:
    try:
        from .auth import resolve_caller_user  # type: ignore
        return resolve_caller_user(authorization, x_user_id)
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="runtime-config authentication backend unavailable") from exc
    except (ImportError, ModuleNotFoundError) as exc:
        logger.error(f"runtime-config authentication module unavailable: {exc}")
        raise HTTPException(status_code=503, detail="runtime-config authentication backend unavailable") from exc

# ── Routes ────────────────────────────────────────────────────────────────────
@router.get("")
@router.get("/")
def get_runtime_config_cp(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    tenant_id: str | None = None,
):
    caller=_resolve_caller(authorization, x_user_id)
    tenant=(tenant_id or x_tenant_id or "default").strip() or "default"
    try:
        snap=fetch_published_snapshot(tenant)
    except RuntimeConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeConfigBackendError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if snap is None:
        # Production fail-closed: missing published config means control plane has no canonical config
        if _is_production():
            raise HTTPException(status_code=503, detail={"code":"NO_PUBLISHED_CONFIG","message":f"no published runtime config for tenant {tenant} — fail-closed"})
        raise HTTPException(status_code=404, detail={"code":"NOT_PUBLISHED","message":f"no published snapshot for tenant {tenant}"})
    # ensure no secret raw leaked (defense-in-depth)
    for prov in snap.get("config",{}).get("llm_providers",[]):
        if "encrypted_api_key" in prov or "api_key" in prov:
            if _is_production():
                raise HTTPException(status_code=503, detail="snapshot contains secret raw — fail-closed")
            raise HTTPException(status_code=502, detail="snapshot contains secret raw — rejected")
    # augment with config_hash/process info
    try:
        ch=snap.get("config_hash") or (_config_hash(snap.get("config",{})) if snap.get("config") else None)
        applied=get_applied(tenant)
    except RuntimeConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeConfigBackendError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"tenant_id": tenant, "snapshot": snap, "verified": True, "caller": caller, "config_hash": ch, "published_version": snap.get("version"), "applied_version": (applied.get("applied_version") if isinstance(applied, dict) else None), "process_identity": _process_identity()}

@router.get("/status")
def get_status_cp(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    tenant_id: str | None = None,
):
    caller=_resolve_caller(authorization, x_user_id)
    tenant=(tenant_id or x_tenant_id or "default").strip() or "default"
    snap=None
    verified=None
    pub_version=None
    config_hash=None
    error=None
    try:
        snap=fetch_published_snapshot(tenant)
        if snap is not None:
            verified=_verify_snapshot(snap)
            pub_version=snap.get("version")
            config_hash=snap.get("config_hash") or _config_hash(snap.get("config",{})) if snap.get("config") else None
        else:
            verified=None
            if _is_production():
                raise HTTPException(status_code=503, detail={"code":"NO_PUBLISHED_CONFIG","message": f"no published runtime config for tenant {tenant} — fail-closed","tenant_id": tenant})
        # also surface tamper as error even if snap is None non-prod
    except HTTPException:
        raise
    except RuntimeConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeConfigBackendError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        applied=get_applied(tenant)
    except RuntimeConfigBackendError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    applied_version=None
    applied_at=None
    applied_ch=None
    applied_error=None
    process_identity=_process_identity()
    if isinstance(applied, dict):
        applied_version=applied.get("applied_version") or applied.get("version")
        applied_at=applied.get("applied_at")
        applied_ch=applied.get("config_hash")
        applied_error=applied.get("error")
        if applied.get("process_identity"):
            process_identity=applied.get("process_identity")
    return {
        "tenant_id": tenant,
        "published_version": pub_version,
        "applied_version": applied_version,
        "config_hash": config_hash or applied_ch,
        "published_hash": config_hash,
        "applied_hash": applied_ch,
        "verified": verified,
        "has_snapshot": snap is not None,
        "applied": applied,
        "process_identity": process_identity,
        "applied_at": applied_at,
        "error": error or applied_error,
        "caller": caller,
    }

@router.post("/apply")
def apply_runtime_config(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    tenant_id: str | None = None,
):
    caller=_resolve_caller(authorization, x_user_id)
    tenant=(tenant_id or x_tenant_id or "default").strip() or "default"
    try:
        snap=fetch_published_snapshot(tenant)
    except RuntimeConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeConfigBackendError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if snap is None:
        if _is_production():
            raise HTTPException(status_code=503, detail="no published config to apply — fail-closed")
        raise HTTPException(status_code=404, detail="no published snapshot")
    if not _verify_snapshot(snap):
        raise HTTPException(status_code=502, detail="snapshot signature invalid — apply rejected")
    # ensure config_hash
    if snap.get("config_hash") is None and snap.get("config"):
        try:
            snap["config_hash"]=_config_hash(snap.get("config",{}))
        except RuntimeConfigValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        rec=mark_applied(tenant, snap, caller)
    except RuntimeConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeConfigBackendError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"tenant_id": tenant, "applied": rec, "snapshot_version": snap.get("version"), "published_version": snap.get("version"), "applied_version": rec.get("applied_version") or rec.get("version"), "config_hash": snap.get("config_hash") or rec.get("config_hash"), "process_identity": rec.get("process_identity"), "applied_at": rec.get("applied_at"), "error": rec.get("error")}

# internal (unauthenticated but tenant-scoped, for health probes)
@internal_router.get("")
@internal_router.get("/")
def internal_get(tenant_id: str = "default", x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id")):
    tenant=(tenant_id or x_tenant_id or "default").strip() or "default"
    try:
        snap=fetch_published_snapshot(tenant)
    except RuntimeConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeConfigBackendError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if snap is None:
        if _is_production():
            raise HTTPException(status_code=503, detail={"code":"NO_PUBLISHED_CONFIG","message":"no published config — fail-closed"})
        return {"tenant_id": tenant, "snapshot": None, "verified": None}
    try:
        verified=_verify_snapshot(snap)
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="malformed runtime config snapshot") from exc
    return {"tenant_id": tenant, "snapshot": snap, "verified": verified}
