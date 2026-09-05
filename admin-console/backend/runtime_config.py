"""Admin Runtime Configuration Plane — Stage-1 minimal vertical slice.

Canonical snapshot stores references only (no secret raw), versioned, HMAC-signed,
tenant-scoped, optimistic concurrency, rollback pointer, audit, approval gate (L5).

Storage: in-memory dict primary + admin_settings K/V mirror (no new table / no migration).
Signing: HMAC-SHA256 over canonical JSON (sorted keys) with
  OAOS_RUNTIME_CONFIG_SIGNING_KEY > ADMIN_JWT_SECRET > dev (prod fail-closed).
Endpoints: GET /v1/runtime/config, POST /snapshot, POST /publish,
           GET /snapshots, GET /snapshots/{version}, POST /rollback, GET /status, GET /audit
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

try:
    from .auth import AdminUser, get_current_admin, require_l5  # type: ignore
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/runtime/config", tags=["runtime-config"])

# ── signing ────────────────────────────────────────────────────────────────────
_DEV_SIGNING_KEY = "dev-runtime-config-signing-key-change-in-prod-32b"

def _is_production() -> bool:
    return os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod")

def _get_signing_key() -> str:
    for k in ("OAOS_RUNTIME_CONFIG_SIGNING_KEY", "OAOS_RC_SIGNING_KEY", "OAOS_SIGNING_KEY", "ADMIN_JWT_SECRET"):
        v = os.environ.get(k, "")
        if v and v.strip():
            if _is_production() and v.strip() == _DEV_SIGNING_KEY:
                raise RuntimeError("OAOS_RUNTIME_CONFIG_SIGNING_KEY must be set to strong value in production (fail-closed)")
            return v.strip()
    if _is_production():
        raise RuntimeError("OAOS_RUNTIME_CONFIG_SIGNING_KEY/ADMIN_JWT_SECRET must be set in production (fail-closed)")
    return _DEV_SIGNING_KEY

def _canonical_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def _sign_canonical(canonical: bytes, key: str | None = None) -> str:
    k = key or _get_signing_key()
    return hmac.new(k.encode("utf-8"), canonical, hashlib.sha256).hexdigest()

def _verify_signature(payload: dict, signature: str, key: str | None = None) -> bool:
    # payload is the config+metadata without signature; signature is hex
    try:
        expected = _sign_canonical(_canonical_bytes(payload), key=key)
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False

def _config_hash(config: dict) -> str:
    try:
        return hashlib.sha256(_canonical_bytes(config)).hexdigest()
    except Exception:
        return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

def _ensure_runtime_tables_sync(engine) -> None:
    try:
        from sqlalchemy import text
        with engine.begin() as conn:
            try:
                conn.execute(text("""CREATE TABLE IF NOT EXISTS admin_runtime_config_snapshots (
                    tenant_id TEXT NOT NULL, version INTEGER NOT NULL, snapshot_json TEXT NOT NULL,
                    signature TEXT NOT NULL, config_hash TEXT NOT NULL, created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL, parent_version INTEGER, rollback_from INTEGER, extra TEXT,
                    PRIMARY KEY (tenant_id, version))""" ))
            except Exception:
                pass
            try:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_rc_snapshots_tenant_created ON admin_runtime_config_snapshots (tenant_id, created_at)" ))
            except Exception:
                pass
            try:
                conn.execute(text("""CREATE TABLE IF NOT EXISTS admin_runtime_config_published (
                    tenant_id TEXT PRIMARY KEY, published_version INTEGER NOT NULL, config_hash TEXT,
                    updated_at TEXT NOT NULL, updated_by TEXT NOT NULL, extra TEXT)""" ))
            except Exception:
                pass
            try:
                conn.execute(text("""CREATE TABLE IF NOT EXISTS admin_runtime_config_applied (
                    tenant_id TEXT PRIMARY KEY, applied_version INTEGER, config_hash TEXT, applied_at TEXT,
                    applied_by TEXT, process_identity TEXT, error TEXT, updated_at TEXT NOT NULL)""" ))
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"ensure runtime tables failed: {e}")

def _db_fetch_snapshot(tenant_id: str, version: int) -> dict | None:
    eng = _db_engine()
    if eng is None:
        return None
    try:
        _ensure_runtime_tables_sync(eng)
        from sqlalchemy import text
        with eng.connect() as conn:
            row = conn.execute(text("SELECT snapshot_json FROM admin_runtime_config_snapshots WHERE tenant_id=:t AND version=:v"), {"t": tenant_id, "v": version}).fetchone()
            if row and row[0]:
                try:
                    return json.loads(row[0])
                except Exception:
                    return None
    except Exception as e:
        logger.debug(f"db fetch snapshot failed: {e}")
    finally:
        try:
            eng.dispose()
        except Exception:
            pass
    return None

def _db_list_snapshots_raw(tenant_id: str) -> list[dict] | None:
    eng = _db_engine()
    if eng is None:
        return None
    try:
        _ensure_runtime_tables_sync(eng)
        from sqlalchemy import text
        with eng.connect() as conn:
            rows = conn.execute(text("SELECT snapshot_json FROM admin_runtime_config_snapshots WHERE tenant_id=:t ORDER BY version ASC"), {"t": tenant_id}).fetchall()
            out=[]
            for r in rows:
                try:
                    out.append(json.loads(r[0]))
                except Exception:
                    continue
            return out if out else []
    except Exception as e:
        logger.debug(f"db list snapshots failed: {e}")
        return None
    finally:
        try:
            eng.dispose()
        except Exception:
            pass

def _db_get_published_raw(tenant_id: str) -> tuple[int | None, str | None]:
    eng = _db_engine()
    if eng is None:
        return None, None
    try:
        _ensure_runtime_tables_sync(eng)
        from sqlalchemy import text
        with eng.connect() as conn:
            row = conn.execute(text("SELECT published_version, config_hash FROM admin_runtime_config_published WHERE tenant_id=:t"), {"t": tenant_id}).fetchone()
            if row:
                return int(row[0]), row[1]
            # fallback legacy admin_settings key
            row2 = conn.execute(text("SELECT value FROM admin_settings WHERE key=:k"), {"k": f"runtime_config:published:{tenant_id}"}).fetchone()
            if row2 and row2[0]:
                try:
                    return int(str(row2[0]).strip()), None
                except Exception:
                    return None, None
    except Exception as e:
        logger.debug(f"db get published raw failed: {e}")
    finally:
        try:
            eng.dispose()
        except Exception:
            pass
    return None, None

def _db_insert_snapshot_durable(tenant_id: str, snapshot: dict) -> bool:
    eng = _db_engine()
    if eng is None:
        return False
    try:
        _ensure_runtime_tables_sync(eng)
        from sqlalchemy import text
        # also mirror legacy admin_settings for backward compat
        with eng.begin() as conn:
            try:
                conn.execute(text("INSERT INTO admin_runtime_config_snapshots (tenant_id, version, snapshot_json, signature, config_hash, created_by, created_at, parent_version, rollback_from) VALUES (:t,:v,:j,:sig,:ch,:by,:at,:pv,:rf)"),
                    {"t": tenant_id, "v": snapshot["version"], "j": json.dumps(snapshot, ensure_ascii=False), "sig": snapshot.get("signature",""), "ch": snapshot.get("config_hash") or _config_hash(snapshot.get("config",{})), "by": snapshot.get("created_by",""), "at": snapshot.get("created_at",""), "pv": snapshot.get("parent_version"), "rf": snapshot.get("rollback_from")})
            except Exception as e:
                # optimistic conflict -> raise to caller as 409
                msg=str(e).lower()
                if "unique" in msg or "primary" in msg or "constraint" in msg or "duplicate" in msg:
                    raise RuntimeError(f"VERSION_CONFLICT:{e}")
                logger.debug(f"db insert durable failed: {e}")
                return False
            # mirror legacy
            try:
                key=f"runtime_config:snapshot:{tenant_id}:{snapshot['version']}"
                val=json.dumps(snapshot, ensure_ascii=False)
                now=_now_iso()
                try:
                    conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES (:k,:v,:now,:by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"), {"k":key,"v":val,"now":now,"by":snapshot.get("created_by")})
                except Exception:
                    conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES (:k,:v,:now,:by)"), {"k":key,"v":val,"now":now,"by":snapshot.get("created_by")})
            except Exception:
                pass
        return True
    except RuntimeError:
        raise
    except Exception as e:
        logger.debug(f"db insert durable outer failed: {e}")
        return False
    finally:
        try:
            eng.dispose()
        except Exception:
            pass

def _db_set_published_durable(tenant_id: str, version: int, config_hash: str | None, actor: str) -> bool:
    eng = _db_engine()
    if eng is None:
        return False
    try:
        _ensure_runtime_tables_sync(eng)
        from sqlalchemy import text
        now=_now_iso()
        with eng.begin() as conn:
            try:
                conn.execute(text("INSERT INTO admin_runtime_config_published (tenant_id, published_version, config_hash, updated_at, updated_by) VALUES (:t,:v,:ch,:now,:by) ON CONFLICT (tenant_id) DO UPDATE SET published_version=:v, config_hash=:ch, updated_at=:now, updated_by=:by"),
                    {"t":tenant_id,"v":version,"ch":config_hash,"now":now,"by":actor})
            except Exception:
                try:
                    conn.execute(text("INSERT OR REPLACE INTO admin_runtime_config_published (tenant_id, published_version, config_hash, updated_at, updated_by) VALUES (:t,:v,:ch,:now,:by)"),
                        {"t":tenant_id,"v":version,"ch":config_hash,"now":now,"by":actor})
                except Exception as e:
                    logger.debug(f"db set published durable failed: {e}")
                    return False
            # also update snapshot rollback flag is handled by caller via _db_mirror_set
        # legacy mirror already done in _db_mirror_published
        return True
    except Exception as e:
        logger.debug(f"db set published durable outer failed: {e}")
        return False
    finally:
        try:
            eng.dispose()
        except Exception:
            pass

def _db_fetch_applied(tenant_id: str) -> dict | None:
    eng = _db_engine()
    if eng is None:
        return None
    try:
        _ensure_runtime_tables_sync(eng)
        from sqlalchemy import text
        with eng.connect() as conn:
            row = conn.execute(text("SELECT applied_version, config_hash, applied_at, applied_by, process_identity, error, updated_at FROM admin_runtime_config_applied WHERE tenant_id=:t"), {"t": tenant_id}).fetchone()
            if row:
                return {"tenant_id": tenant_id, "applied_version": row[0], "config_hash": row[1], "applied_at": row[2], "applied_by": row[3], "process_identity": row[4], "error": row[5], "updated_at": row[6]}
    except Exception as e:
        logger.debug(f"db fetch applied failed: {e}")
    finally:
        try:
            eng.dispose()
        except Exception:
            pass
    return None

# ── helpers collecting current live config (references only) ───────────────────
# Canonical OAOS runtime: Hermes Gateway 127.0.0.1:8642 with model from OAOS_CP_HERMES_MODEL.
# Prior DB snapshots used http://localhost:8642 + qwen2.5 (stale/mismatched). Fix prefers
# effective env and canonical loopback, normalizing localhost->127.0.0.1 and :8001->:8642.
_CANONICAL_HERMES_BASE_URL = "http://127.0.0.1:8642"
_CANONICAL_HERMES_DEFAULT_MODEL = "muse-spark-1.2-contributor"

def _collect_runtime_mode() -> str:
    try:
        try:
            from .runtime_mode import get_mode  # type: ignore
        except ImportError:
            from runtime_mode import get_mode  # type: ignore
        m = get_mode()
        return m.value if hasattr(m, "value") else str(m)
    except Exception:
        return os.environ.get("OAOS_RUNTIME_MODE", "hermes")

def _normalize_hermes_base_url(raw: str) -> str:
    v = (raw or "").strip()
    if not v:
        return v
    if "localhost" in v:
        v = v.replace("localhost", "127.0.0.1")
    if v == "http://127.0.0.1:8001":
        v = _CANONICAL_HERMES_BASE_URL
    return v

def _collect_hermes() -> dict:
    observed_at = _now_iso()
    env_base = (os.environ.get("OAOS_CP_HERMES_BASE_URL") or os.environ.get("HERMES_BASE_URL") or "").strip()
    env_model = (os.environ.get("OAOS_CP_HERMES_MODEL") or os.environ.get("HERMES_MODEL") or "").strip()
    base = ""
    model = ""
    source_base = ""
    source_model = ""
    if env_base:
        base = _normalize_hermes_base_url(env_base)
        source_base = "env:OAOS_CP_HERMES_BASE_URL" if os.environ.get("OAOS_CP_HERMES_BASE_URL", "").strip() else "env:HERMES_BASE_URL"
    if env_model:
        model = env_model
        source_model = "env:OAOS_CP_HERMES_MODEL" if os.environ.get("OAOS_CP_HERMES_MODEL", "").strip() else "env:HERMES_MODEL"
    # Stale control-plane defaults (localhost:8001 / qwen2.5) must not override canonical
    _STALE_MODELS = {"qwen2.5", "qwen2.5:qwen2.5", "qwen"}
    if not base or not model:
        try:
            from control_plane.config import settings as cp_settings  # type: ignore
            if not base:
                cb = (getattr(cp_settings, "hermes_base_url", "") or "").strip()
                if cb:
                    nb = _normalize_hermes_base_url(cb)
                    # treat stale :8001/qwen-era defaults as non-authoritative
                    if nb != _CANONICAL_HERMES_BASE_URL or cb.strip() not in ("http://localhost:8001", "http://127.0.0.1:8001"):
                        base = nb
                        source_base = "control_plane.config:hermes_base_url"
                    elif nb == _CANONICAL_HERMES_BASE_URL:
                        # :8001 normalized to canonical is still stale-control-plane; prefer canonical default
                        pass
                    else:
                        base = nb
                        source_base = "control_plane.config:hermes_base_url"
            if not model:
                cm = (getattr(cp_settings, "hermes_model", "") or "").strip()
                if cm and cm.lower() not in _STALE_MODELS:
                    model = cm
                    source_model = "control_plane.config:hermes_model"
                elif cm:
                    # stale qwen2.5 — ignore, fall through to canonical default
                    pass
        except Exception:
            pass
    if not base:
        base = _CANONICAL_HERMES_BASE_URL
        source_base = "default:canonical"
    if not model:
        model = _CANONICAL_HERMES_DEFAULT_MODEL
        source_model = "default:canonical" if not source_model else source_model
    source = source_base
    if source_model and source_model != source_base:
        source = f"{source_base}+{source_model}" if source_base else source_model
    return {"base_url": base, "model": model, "source": source or "default:canonical", "observed_at": observed_at}

_last_llm_providers_meta: dict = {"source": "unknown", "observed_at": "", "inventory_status": "unknown"}

def _collect_llm_providers() -> list[dict]:
    global _last_llm_providers_meta
    observed_at = _now_iso()
    items: list[dict] = []
    source = "unknown"
    inventory_status = "unknown"
    try:
        mod = None
        import sys
        for name in ("admin_console.backend.llm_providers", "llm_providers", "admin_llm_providers"):
            m = sys.modules.get(name)
            if m is not None and hasattr(m, "_providers"):
                mod = m
                break
        if mod is None:
            try:
                from . import llm_providers as _lp  # type: ignore
                mod = _lp
            except Exception:
                try:
                    import llm_providers as _lp2  # type: ignore
                    mod = _lp2
                except Exception:
                    mod = None
        if mod is not None:
            source = "in-memory:llm_providers"
            providers = None
            if hasattr(mod, "_db_list_providers"):
                try:
                    providers = mod._db_list_providers()
                    source = "db:llm_providers" if providers is not None else source
                except Exception:
                    providers = None
            if providers is None:
                providers = list(getattr(mod, "_providers", {}).values())
            for p in providers:
                try:
                    d = p.model_dump(mode="json") if hasattr(p, "model_dump") else dict(p)  # type: ignore
                except Exception:
                    d = {}
                items.append({
                    "id": d.get("id", ""),
                    "provider": str(d.get("provider", "")),
                    "name": d.get("name", ""),
                    "model": d.get("model"),
                    "base_url": d.get("base_url"),
                    "path": d.get("path"),
                    "url": d.get("url"),
                    "enabled": bool(d.get("enabled", True)),
                    "secret_ref": d.get("secret_ref"),
                    "vault_backend": d.get("vault_backend"),
                })
            inventory_status = "populated" if items else "empty:observed:zero-providers"
        else:
            source = "unavailable:llm_providers-module-not-loaded"
            inventory_status = "empty:unobserved:module-not-loaded"
    except Exception as e:
        logger.debug(f"collect llm providers failed: {e}")
        source = "error:llm_providers-collect-failed"
        inventory_status = "empty:unobserved:collect-error"
    if inventory_status == "unknown":
        inventory_status = "populated" if items else "empty:observed:zero-providers"
    _last_llm_providers_meta = {"source": source, "observed_at": observed_at, "inventory_status": inventory_status, "count": len(items)}
    return items

def _collect_llm_providers_meta() -> dict:
    return dict(_last_llm_providers_meta)

def _collect_fallback() -> dict:
    observed_at = _now_iso()
    source = "unknown"
    inventory_status = "unknown"
    try:
        import sys
        mod = None
        for name in ("admin_console.backend.fallback", "fallback"):
            m = sys.modules.get(name)
            if m is not None and hasattr(m, "_load_config"):
                mod = m
                break
        if mod is None:
            try:
                from . import fallback as _fb  # type: ignore
                mod = _fb
            except Exception:
                try:
                    import fallback as _fb2  # type: ignore
                    mod = _fb2
                except Exception:
                    mod = None
        if mod is not None:
            cfg = mod._load_config()  # type: ignore
            d = cfg.model_dump(mode="json") if hasattr(cfg, "model_dump") else {}
            source = "db:fallback" if hasattr(mod, "_load_config") else "in-memory:fallback"
            chain = d.get("chain", [])
            inventory_status = "populated" if chain else "empty:observed:zero-chain"
            return {"enabled": bool(d.get("enabled", True)), "chain": chain, "fallback_model": d.get("fallback_model"), "source": source, "observed_at": observed_at, "inventory_status": inventory_status}
    except Exception as e:
        logger.debug(f"collect fallback failed: {e}")
        source = "error:fallback-collect-failed"
        inventory_status = "empty:unobserved:collect-error"
    if inventory_status == "unknown":
        source = source if source != "unknown" else "unavailable:fallback-module-not-loaded"
        inventory_status = "empty:unobserved:module-not-loaded" if source.startswith("unavailable") else "empty:observed:zero-chain"
    return {"enabled": True, "chain": [], "fallback_model": None, "source": source, "observed_at": observed_at, "inventory_status": inventory_status}

def _collect_infra() -> dict:
    observed_at = _now_iso()
    services: list[dict] = []
    source = "unknown"
    inventory_status = "unknown"
    try:
        import sys
        mod = None
        for name in ("admin_console.backend.infra", "infra", "admin_infra"):
            m = sys.modules.get(name)
            if m is not None and hasattr(m, "_services"):
                mod = m
                break
        if mod is None:
            try:
                from . import infra as _im  # type: ignore
                mod = _im
            except Exception:
                try:
                    import infra as _im2  # type: ignore
                    mod = _im2
                except Exception:
                    mod = None
        if mod is not None:
            lst = None
            if hasattr(mod, "_db_list_services"):
                try:
                    lst = mod._db_list_services()
                    if lst is not None:
                        source = "db:admin_infra_services"
                    else:
                        source = "in-memory:infra"
                except Exception:
                    lst = None
                    source = "in-memory:infra"
            if lst is None:
                lst = list(getattr(mod, "_services", {}).values())
                if source == "unknown":
                    source = "in-memory:infra"
            for s in lst:
                try:
                    d = s.model_dump(mode="json") if hasattr(s, "model_dump") else dict(s)
                except Exception:
                    d = {}
                services.append({
                    "id": d.get("id"),
                    "name": d.get("name"),
                    "display_name": d.get("display_name"),
                    "host": d.get("host"),
                    "port": d.get("port"),
                    "health_path": d.get("health_path"),
                    "expected_status": d.get("expected_status"),
                    "status": str(d.get("status", "unknown")),
                })
            if services:
                inventory_status = "populated"
            else:
                inventory_status = "empty:observed:zero-services"
        else:
            source = "unavailable:infra-module-not-loaded"
            inventory_status = "empty:unobserved:module-not-loaded"
    except Exception as e:
        logger.debug(f"collect infra failed: {e}")
        source = "error:infra-collect-failed"
        inventory_status = "empty:unobserved:collect-error"
    h = hashlib.sha256(json.dumps(sorted([sv.get("id","") for sv in services]), separators=(",",":"), ensure_ascii=False).encode()).hexdigest()[:12] if services else "empty"
    return {"count": len(services), "hash": h, "services": services, "source": source, "observed_at": observed_at, "inventory_status": inventory_status}

def _collect_user_mappings() -> dict:
    observed_at = _now_iso()
    mappings: list[dict] = []
    source = "unknown"
    inventory_status = "unknown"
    try:
        import sys
        mod = None
        for name in ("admin_console.backend.user_mappings", "user_mappings", "admin_user_mappings"):
            m = sys.modules.get(name)
            if m is not None and hasattr(m, "_mappings"):
                mod = m
                break
        if mod is None:
            try:
                from . import user_mappings as _um  # type: ignore
                mod = _um
            except Exception:
                try:
                    import user_mappings as _um2  # type: ignore
                    mod = _um2
                except Exception:
                    mod = None
        if mod is not None:
            lst = None
            if hasattr(mod, "_db_list_mappings"):
                try:
                    lst = mod._db_list_mappings()
                    if lst is not None:
                        source = "db:admin_user_mappings"
                    else:
                        source = "in-memory:user_mappings"
                except Exception:
                    lst = None
                    source = "in-memory:user_mappings"
            if lst is None:
                lst = list(getattr(mod, "_mappings", {}).values())
                if source == "unknown":
                    source = "in-memory:user_mappings"
            for m in lst:
                try:
                    d = m.model_dump(mode="json") if hasattr(m, "model_dump") else dict(m)
                except Exception:
                    d = {}
                mappings.append({
                    "id": d.get("id"),
                    "mm_user_id": d.get("mm_user_id"),
                    "mm_username": d.get("mm_username"),
                    "employee_principal": d.get("employee_principal"),
                    "agent_id": d.get("agent_id"),
                })
            if mappings:
                inventory_status = "populated"
            else:
                inventory_status = "empty:observed:zero-mappings"
        else:
            source = "unavailable:user_mappings-module-not-loaded"
            inventory_status = "empty:unobserved:module-not-loaded"
    except Exception as e:
        logger.debug(f"collect mappings failed: {e}")
        source = "error:user_mappings-collect-failed"
        inventory_status = "empty:unobserved:collect-error"
    h = hashlib.sha256(json.dumps(sorted([mm.get("id","") for mm in mappings]), separators=(",",":"), ensure_ascii=False).encode()).hexdigest()[:12] if mappings else "empty"
    return {"count": len(mappings), "hash": h, "mappings": mappings, "source": source, "observed_at": observed_at, "inventory_status": inventory_status}

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _tenant_of(principal: str | None, fallback: str = "default") -> str:
    # AdminUser has no tenant; use X-Tenant-Id header or default
    return (principal or fallback or "default").strip() or "default"

# ── in-memory stores (primary for stage-1) ────────────────────────────────────
# tenant -> {version -> snapshot dict}
_snapshots: dict[str, dict[int, dict]] = {}
# tenant -> published_version (int | None)
_published: dict[str, int | None] = {}
# audit events (stage-1 local, mirrored to security audit_ledger if available)
_audit_events: list[dict] = []
_MAX_AUDIT = 500

def _audit(tenant_id: str, version: int, action: str, actor: str, signature: str) -> None:
    ev = {
        "event_id": f"ae_{uuid.uuid4().hex[:10]}",
        "tenant_id": tenant_id,
        "version": version,
        "action": action,
        "actor": actor,
        "signature": signature[:16] + "..." if signature else "",
        "timestamp": _now_iso(),
        "process": f"{socket.gethostname()}:{os.getpid()}",
    }
    _audit_events.append(ev)
    if len(_audit_events) > _MAX_AUDIT:
        del _audit_events[: len(_audit_events) - _MAX_AUDIT]
    # best-effort mirror to security audit_ledger
    try:
        import security.app as sec  # type: ignore
        ledger = getattr(sec, "audit_ledger", None)
        if ledger is not None and hasattr(ledger, "append"):
            try:
                ledger.append(ev)  # type: ignore
            except Exception:
                pass
    except Exception:
        pass

# ── DB mirror via admin_settings (no new table, idempotent) ──────────────────
def _db_url() -> str | None:
    for name in ("OAOS_DATABASE_URL", "DATABASE_URL"):
        v = os.environ.get(name, "")
        if v and v.strip():
            return v.strip()
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
    return None

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

def _db_engine():
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
        return create_engine(sync_url, **kwargs)
    except Exception as e:
        logger.debug(f"runtime_config DB engine failed: {e}")
        return None

def _db_mirror_set(tenant_id: str, version: int, snapshot: dict) -> None:
    eng = _db_engine()
    if eng is None:
        return
    try:
        from sqlalchemy import text
        key = f"runtime_config:snapshot:{tenant_id}:{version}"
        val = json.dumps(snapshot, ensure_ascii=False)
        with eng.begin() as conn:
            try:
                conn.execute(text("CREATE TABLE IF NOT EXISTS admin_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT, updated_by TEXT, extra TEXT)"))
            except Exception:
                pass
            now = _now_iso()
            try:
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES (:k, :v, :now, :by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"),
                             {"k": key, "v": val, "now": now, "by": snapshot.get("created_by")})
            except Exception:
                try:
                    conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES (:k, :v, :now, :by)"),
                                 {"k": key, "v": val, "now": now, "by": snapshot.get("created_by")})
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"runtime_config DB mirror snapshot failed: {e}")
    finally:
        try:
            eng.dispose()
        except Exception:
            pass

def _db_mirror_published(tenant_id: str, version: int, actor: str) -> None:
    eng = _db_engine()
    if eng is None:
        return
    try:
        from sqlalchemy import text
        with eng.begin() as conn:
            try:
                conn.execute(text("CREATE TABLE IF NOT EXISTS admin_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT, updated_by TEXT, extra TEXT)"))
            except Exception:
                pass
            now = _now_iso()
            key = f"runtime_config:published:{tenant_id}"
            try:
                conn.execute(text("INSERT INTO admin_settings (key, value, updated_at, updated_by) VALUES (:k, :v, :now, :by) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=:now, updated_by=:by"),
                             {"k": key, "v": str(version), "now": now, "by": actor})
            except Exception:
                conn.execute(text("INSERT OR REPLACE INTO admin_settings (key, value, updated_at, updated_by) VALUES (:k, :v, :now, :by)"),
                             {"k": key, "v": str(version), "now": now, "by": actor})
    except Exception as e:
        logger.debug(f"runtime_config DB mirror published failed: {e}")
    finally:
        try:
            eng.dispose()
        except Exception:
            pass

def _is_destructive_db_allowed() -> bool:
    # Guard: never wipe production DB from tests. Tests must use isolated sqlite.
    # Defense-in-depth: production check first, sqlite-only allow, explicit flag still requires sqlite.
    if _is_production():
        return False
    url = (_db_url() or "").strip().lower()
    if url.startswith("sqlite"):
        return True
    if os.environ.get("OAOS_ALLOW_DESTRUCTIVE_RUNTIME_CONFIG_CLEAR") == "1":
        # still require sqlite even with flag
        return url.startswith("sqlite")
    if not url:
        # in-memory only — no DB to wipe, in-mem clear is fine
        return False  # skip DB branch; caller will still have cleared dicts
    # Any postgres/mysql etc is treated as production-like → block destructive clear
    return False

def _collect_observed_system_inventory() -> dict:
    observed_at = _now_iso()
    # Read-only static inventory (non-secret) — never writes live health into desired infra.
    # infra (desired) stays as managed admin_infra_services; observed is separate.
    try:
        import sys as _sys
        for _name in ("admin_console.backend.infra", "infra", "admin_infra"):
            _m = _sys.modules.get(_name)
            if _m is not None and hasattr(_m, "LIVE_INVENTORY"):
                try:
                    live = getattr(_m, "LIVE_INVENTORY") or []
                    items = [
                        {
                            "id": e.get("id"),
                            "name": e.get("name"),
                            "display_name": e.get("display_name") or e.get("name"),
                            "host": e.get("host"),
                            "port": e.get("port"),
                            "health_path": e.get("health_path", "/health"),
                        }
                        for e in live
                    ]
                    return {
                        "source": f"static:{_name}:LIVE_INVENTORY",
                        "observed_at": observed_at,
                        "inventory_status": "observed:static:list",
                        "count": len(items),
                        "items": items,
                    }
                except Exception:
                    pass
        try:
            from . import infra as _im  # type: ignore
            if hasattr(_im, "LIVE_INVENTORY"):
                live = getattr(_im, "LIVE_INVENTORY") or []
                items = [
                    {
                        "id": e.get("id"),
                        "name": e.get("name"),
                        "display_name": e.get("display_name") or e.get("name"),
                        "host": e.get("host"),
                        "port": e.get("port"),
                        "health_path": e.get("health_path", "/health"),
                    }
                    for e in live
                ]
                return {
                    "source": "static:infra:LIVE_INVENTORY",
                    "observed_at": observed_at,
                    "inventory_status": "observed:static:list",
                    "count": len(items),
                    "items": items,
                }
        except Exception:
            pass
    except Exception:
        pass
    # Fallback static canonical list (non-secret, no probing)
    fallback = [
        {"id": "live_cp", "name": "control-plane", "host": "127.0.0.1", "port": 8100, "health_path": "/health"},
        {"id": "live_memory", "name": "memory", "host": "127.0.0.1", "port": 8200, "health_path": "/health"},
        {"id": "live_hermes", "name": "hermes", "host": "127.0.0.1", "port": 8642, "health_path": "/health"},
        {"id": "live_admin_api", "name": "admin-api", "host": "127.0.0.1", "port": 8010, "health_path": "/health"},
    ]
    return {
        "source": "static:fallback:LIVE_INVENTORY",
        "observed_at": observed_at,
        "inventory_status": "observed:static:list",
        "count": len(fallback),
        "items": fallback,
    }

# ── public helpers (also used by tests / CP) ────────────────────────────────
def clear_runtime_config_state() -> None:
    return clear_runtime_config()

def clear_runtime_config() -> None:
    _snapshots.clear()
    _published.clear()
    _audit_events.clear()
    # also clear DB durable + mirrors (best-effort) — with production guard
    if not _is_destructive_db_allowed():
        # In production / non-sqlite DB, only in-memory is cleared; DB is preserved.
        # Emit debug so tests that forgot to isolate are visible.
        logger.debug("clear_runtime_config: destructive DB clear skipped (production guard / non-sqlite URL)")
        return
    eng = _db_engine()
    if eng is not None:
        try:
            from sqlalchemy import text
            _ensure_runtime_tables_sync(eng)
            with eng.begin() as conn:
                try:
                    conn.execute(text("DELETE FROM admin_settings WHERE key LIKE 'runtime_config:%'"))
                except Exception:
                    pass
                try:
                    conn.execute(text("DELETE FROM admin_runtime_config_snapshots WHERE 1=1"))
                except Exception:
                    pass
                try:
                    conn.execute(text("DELETE FROM admin_runtime_config_published WHERE 1=1"))
                except Exception:
                    pass
                try:
                    conn.execute(text("DELETE FROM admin_runtime_config_applied WHERE 1=1"))
                except Exception:
                    pass
        except Exception:
            pass
        try:
            eng.dispose()
        except Exception:
            pass

def _max_version(tenant_id: str) -> int:
    m = _snapshots.get(tenant_id, {})
    return max(m.keys()) if m else 0

def _build_snapshot(tenant_id: str, actor: str, version: int, parent_version: int | None) -> dict:
    hermes_cfg = _collect_hermes()
    llm_providers_cfg = _collect_llm_providers()
    fallback_cfg = _collect_fallback()
    infra_cfg = _collect_infra()
    user_mappings_cfg = _collect_user_mappings()
    observed_inventory_cfg = _collect_observed_system_inventory()
    config = {
        "runtime_mode": _collect_runtime_mode(),
        "hermes": hermes_cfg,
        "llm_providers": llm_providers_cfg,
        "fallback": fallback_cfg,
        "infra": infra_cfg,
        "user_mappings": user_mappings_cfg,
        "observed_system_inventory": observed_inventory_cfg,
        # Additive IA alias references (no secrets; old readers ignore unknown keys)
        "process_aliases": {
            "mattermost_adapter": {
                "canonical": "oaos-adapter-mattermost.service",
                "aliases": ["oaos-mm-bridge.service"],
            },
            "governance": {
                "canonical": "oaos-governance.service",
                "aliases": ["oaos-security.service"],
            },
        },
    }
    # Additive provenance for llm_providers (list contract preserved)
    try:
        llm_meta = _collect_llm_providers_meta()
        config["llm_providers_source"] = llm_meta.get("source")
        config["llm_providers_observed_at"] = llm_meta.get("observed_at")
        config["llm_providers_inventory_status"] = llm_meta.get("inventory_status")
        config["llm_providers_count"] = llm_meta.get("count")
    except Exception:
        pass
    # Guard: never store secret raw
    for prov in config.get("llm_providers", []):
        if "encrypted_api_key" in prov or "api_key" in prov or "apiKey" in prov:
            raise RuntimeError("secret raw must not be stored in snapshot")
        # ensure secret_ref present for apiKey providers when enabled? not mandatory, but warn if missing
    ch = _config_hash(config)
    payload_without_sig = {
        "tenant_id": tenant_id,
        "version": version,
        "created_at": _now_iso(),
        "created_by": actor,
        "parent_version": parent_version,
        "config": config,
        "config_hash": ch,
    }
    sig = _sign_canonical(_canonical_bytes(payload_without_sig))
    snapshot = {
        **payload_without_sig,
        "signature": sig,
        "published": False,
        "published_at": None,
        "published_by": None,
        "rollback_from": None,
    }
    return snapshot

def get_published_snapshot(tenant_id: str = "default") -> dict | None:
    # DB primary: try durable published pointer first
    try:
        pub_ver, _ = _db_get_published_raw(tenant_id)
        if pub_ver is not None:
            snap = _db_fetch_snapshot(tenant_id, pub_ver)
            if snap is not None:
                return snap
            # fallback to in-memory if DB snapshot missing but published pointer exists
            return _snapshots.get(tenant_id, {}).get(pub_ver)
    except Exception:
        pass
    ver = _published.get(tenant_id)
    if ver is None:
        return None
    # also try DB fetch if in-memory miss
    snap = _snapshots.get(tenant_id, {}).get(ver)
    if snap is None:
        try:
            snap = _db_fetch_snapshot(tenant_id, ver)
        except Exception:
            pass
    return snap

def list_snapshots(tenant_id: str = "default") -> list[dict]:
    # DB primary if available
    try:
        db_items = _db_list_snapshots_raw(tenant_id)
        if db_items is not None and len(db_items) > 0:
            # merge with in-memory for completeness (in-mem may have newer not yet flushed)
            # prefer DB as canonical
            return sorted(db_items, key=lambda x: x.get("version",0))
    except Exception:
        pass
    m = _snapshots.get(tenant_id, {})
    return [m[v] for v in sorted(m.keys())]

# ── Pydantic request models ───────────────────────────────────────────────────
class SnapshotRequest(BaseModel):
    tenant_id: Optional[str] = None
    expected_version: Optional[int] = None
    note: Optional[str] = None

class PublishRequest(BaseModel):
    tenant_id: Optional[str] = None
    version: int

class RollbackRequest(BaseModel):
    tenant_id: Optional[str] = None
    version: int

def _resolve_tenant(body_tenant: str | None, header_tenant: str | None) -> str:
    raw = (body_tenant or header_tenant or "default").strip()
    if not raw:
        raw = "default"
    # sanitize tenant scope (alphanum + - _)
    if len(raw) > 64:
        raise HTTPException(status_code=400, detail="tenant_id too long (max 64)")
    return raw

# ── Routes ────────────────────────────────────────────────────────────────────
@router.get("/")
@router.get("")
def get_runtime_config(
    admin: AdminUser = Depends(get_current_admin),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    tenant_id: str | None = None,
):
    # tenant scope: support both query ?tenant_id= and header X-Tenant-Id (header preferred via _resolve_tenant ordering)
    tenant = _resolve_tenant(tenant_id, x_tenant_id)
    # fail-closed in prod if no snapshot ever published? Return 404 with header, not 503
    snap = get_published_snapshot(tenant)
    if snap is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_PUBLISHED", "message": f"no published snapshot for tenant {tenant}"})
    # verify signature before returning (fail-closed)
    payload = {k: v for k, v in snap.items() if k not in ("signature", "published", "published_at", "published_by", "rollback_from")}
    if not _verify_signature(payload, snap.get("signature", "")):
        if _is_production():
            raise HTTPException(status_code=503, detail="published snapshot signature invalid — fail-closed")
        raise HTTPException(status_code=502, detail="published snapshot signature invalid")
    return snap

@router.post("/snapshot", status_code=201)
def create_snapshot(
    body: SnapshotRequest,
    admin: AdminUser = Depends(require_l5),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
):
    # signing key validation (fail-closed)
    try:
        _get_signing_key()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    # DB primary: determine current max via DB + memory merge
    tenant = _resolve_tenant(body.tenant_id, x_tenant_id)
    # Try DB list to get authoritative max
    try:
        db_items = _db_list_snapshots_raw(tenant)
        if db_items is not None:
            db_max = max([s.get("version",0) for s in db_items], default=0)
            cur_max = max(_max_version(tenant), db_max)
        else:
            cur_max = _max_version(tenant)
    except Exception:
        cur_max = _max_version(tenant)
    expected = body.expected_version
    if expected is not None:
        if expected != cur_max + 1:
            raise HTTPException(status_code=409, detail={"code": "VERSION_CONFLICT", "message": f"expected_version {expected} != next {cur_max+1}", "current_version": cur_max})
        version = expected
    else:
        version = cur_max + 1
    # guard optimistic: if version already exists in either store
    if version in _snapshots.get(tenant, {}):
        raise HTTPException(status_code=409, detail={"code": "VERSION_CONFLICT", "message": f"version {version} already exists"})
    try:
        if _db_fetch_snapshot(tenant, version) is not None:
            raise HTTPException(status_code=409, detail={"code": "VERSION_CONFLICT", "message": f"version {version} already exists (DB)"})
    except HTTPException:
        raise
    except Exception:
        pass
    snapshot = _build_snapshot(tenant, admin.email, version, parent_version=cur_max if cur_max else None)
    # DB primary transaction (optimistic) — if DB available, insert durable first
    try:
        eng = _db_engine()
        if eng is not None:
            _db_insert_snapshot_durable(tenant, snapshot)
    except RuntimeError as re:
        if "VERSION_CONFLICT" in str(re):
            raise HTTPException(status_code=409, detail={"code": "VERSION_CONFLICT", "message": str(re)})
    except Exception as e:
        if _is_production():
            raise HTTPException(status_code=503, detail="runtime snapshot durable DB write failed — fail-closed")
        logger.debug(f"snapshot DB primary failed (fallback to mem): {e}")
    _snapshots.setdefault(tenant, {})[version] = snapshot
    # DB mirror legacy (also done inside durable)
    try:
        _db_mirror_set(tenant, version, snapshot)
    except Exception:
        pass
    _audit(tenant, version, "snapshot_created", admin.email, snapshot["signature"])
    return snapshot

@router.post("/publish")
def publish_snapshot(
    body: PublishRequest,
    admin: AdminUser = Depends(require_l5),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
):
    tenant = _resolve_tenant(body.tenant_id, x_tenant_id)
    # Resolve snapshot via DB primary if not in memory
    m = _snapshots.get(tenant, {})
    snap = m.get(body.version)
    if snap is None:
        try:
            snap = _db_fetch_snapshot(tenant, body.version)
            if snap is not None:
                _snapshots.setdefault(tenant, {})[body.version] = snap
                m = _snapshots.get(tenant, {})
        except Exception:
            pass
    if snap is None:
        raise HTTPException(status_code=404, detail=f"version {body.version} not found for tenant {tenant}")
    # verify signature before publish (fail-closed)
    payload = {k: v for k, v in snap.items() if k not in ("signature", "published", "published_at", "published_by", "rollback_from", "config_hash")}
    # fallback: also support old payload without config_hash
    if not _verify_signature(payload, snap.get("signature", "")):
        # try legacy payload without config_hash
        payload_legacy = {k: v for k, v in snap.items() if k not in ("signature", "published", "published_at", "published_by", "rollback_from")}
        if not _verify_signature(payload_legacy, snap.get("signature", "")):
            raise HTTPException(status_code=502, detail="snapshot signature invalid — publish rejected")
    # publish pointer — DB primary
    prev = _published.get(tenant)
    try:
        pub_ver, _ = _db_get_published_raw(tenant)
        if pub_ver is not None:
            prev = pub_ver if prev is None else prev
    except Exception:
        pass
    # DB durable first
    ch = snap.get("config_hash") or _config_hash(snap.get("config",{}))
    try:
        _db_set_published_durable(tenant, body.version, ch, admin.email)
    except Exception as e:
        logger.debug(f"publish durable failed: {e}")
    _published[tenant] = body.version
    snap["published"] = True
    snap["published_at"] = _now_iso()
    snap["published_by"] = admin.email
    snap["config_hash"] = ch
    # clear old published flag
    for ver, s in list(m.items()):
        if ver != body.version:
            s["published"] = False
            try:
                _db_mirror_set(tenant, ver, s)
            except Exception:
                pass
    _db_mirror_published(tenant, body.version, admin.email)
    try:
        _db_mirror_set(tenant, body.version, snap)
        # also update durable snapshot JSON for published flag (+ config_hash)
        try:
            _db_insert_snapshot_durable(tenant, snap)
        except Exception:
            # upsert snapshot_json directly if insert conflict
            try:
                eng = _db_engine()
                if eng is not None:
                    from sqlalchemy import text as _t
                    _ensure_runtime_tables_sync(eng)
                    with eng.begin() as conn:
                        conn.execute(_t("UPDATE admin_runtime_config_snapshots SET snapshot_json=:j, config_hash=:ch WHERE tenant_id=:t AND version=:v"), {"j": __import__("json").dumps(snap, ensure_ascii=False), "ch": ch, "t": tenant, "v": body.version})
                    eng.dispose()
            except Exception:
                pass
    except Exception:
        pass
    _audit(tenant, body.version, "published", admin.email, snap["signature"])
    return {"tenant_id": tenant, "published_version": body.version, "previous_version": prev, "snapshot": snap, "config_hash": ch}

@router.get("/snapshots")
def list_snapshots_api(
    admin: AdminUser = Depends(get_current_admin),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    tenant_id: str | None = None,
):
    tenant = _resolve_tenant(tenant_id, x_tenant_id)
    items = list_snapshots(tenant)
    return {"tenant_id": tenant, "count": len(items), "items": items}

@router.get("/snapshots/{version}")
def get_snapshot_version(
    version: int,
    admin: AdminUser = Depends(get_current_admin),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    tenant_id: str | None = None,
):
    tenant = _resolve_tenant(tenant_id, x_tenant_id)
    snap = _snapshots.get(tenant, {}).get(version)
    if snap is None:
        try:
            snap = _db_fetch_snapshot(tenant, version)
            if snap is not None:
                _snapshots.setdefault(tenant, {})[version] = snap
        except Exception:
            snap = None
    if snap is None:
        raise HTTPException(status_code=404, detail=f"version {version} not found")
    return snap

@router.post("/rollback")
def rollback_snapshot(
    body: RollbackRequest,
    admin: AdminUser = Depends(require_l5),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
):
    tenant = _resolve_tenant(body.tenant_id, x_tenant_id)
    m = _snapshots.get(tenant, {})
    target = m.get(body.version)
    if target is None:
        try:
            target = _db_fetch_snapshot(tenant, body.version)
            if target is not None:
                _snapshots.setdefault(tenant, {})[body.version] = target
                m = _snapshots.get(tenant, {})
        except Exception:
            pass
    if target is None:
        raise HTTPException(status_code=404, detail=f"version {body.version} not found for tenant {tenant}")
    current_published = _published.get(tenant)
    try:
        pub_ver, _ = _db_get_published_raw(tenant)
        if pub_ver is not None:
            current_published = pub_ver if current_published is None else current_published
            # authoritative is DB if present
            if pub_ver != _published.get(tenant):
                current_published = pub_ver
    except Exception:
        pass
    if current_published is None:
        raise HTTPException(status_code=409, detail="no published version to rollback from")
    if current_published == body.version:
        raise HTTPException(status_code=409, detail="already at requested version")
    # mark target as published, record rollback_from
    target["rollback_from"] = current_published
    target["published"] = True
    target["published_at"] = _now_iso()
    target["published_by"] = admin.email
    ch = target.get("config_hash") or _config_hash(target.get("config",{}))
    target["config_hash"] = ch
    for ver, s in list(m.items()):
        if ver != body.version:
            s["published"] = False
            try:
                _db_mirror_set(tenant, ver, s)
            except Exception:
                pass
    _published[tenant] = body.version
    try:
        _db_set_published_durable(tenant, body.version, ch, admin.email)
    except Exception:
        pass
    _db_mirror_published(tenant, body.version, admin.email)
    try:
        _db_mirror_set(tenant, body.version, target)
        try:
            eng = _db_engine()
            if eng is not None:
                from sqlalchemy import text as _t
                _ensure_runtime_tables_sync(eng)
                with eng.begin() as conn:
                    conn.execute(_t("UPDATE admin_runtime_config_snapshots SET snapshot_json=:j, config_hash=:ch WHERE tenant_id=:t AND version=:v"), {"j": __import__("json").dumps(target, ensure_ascii=False), "ch": ch, "t": tenant, "v": body.version})
                eng.dispose()
        except Exception:
            pass
    except Exception:
        pass
    _audit(tenant, body.version, "rollback", admin.email, target.get("signature",""))
    return {"tenant_id": tenant, "published_version": body.version, "rolled_back_from": current_published, "snapshot": target, "config_hash": ch}

@router.get("/status")
def get_status_api(
    admin: AdminUser = Depends(get_current_admin),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    tenant_id: str | None = None,
):
    tenant = _resolve_tenant(tenant_id, x_tenant_id)
    # Try DB primary for published
    pub = _published.get(tenant)
    try:
        db_pub, _ = _db_get_published_raw(tenant)
        if db_pub is not None:
            pub = db_pub
    except Exception:
        pass
    snap = get_published_snapshot(tenant) if pub is not None else None
    # also try DB fetch if snap missing
    if snap is None and pub is not None:
        try:
            snap = _db_fetch_snapshot(tenant, pub)
        except Exception:
            pass
    # applied info from DB (written by Control Plane)
    applied = None
    try:
        applied = _db_fetch_applied(tenant)
    except Exception:
        applied = None
    sig_valid = None
    ch = None
    if snap is not None:
        ch = snap.get("config_hash") or _config_hash(snap.get("config",{}))
        # verify includes config_hash now
        payload = {k: v for k, v in snap.items() if k not in ("signature","published","published_at","published_by","rollback_from","config_hash")}
        # legacy fallback
        sig_valid = _verify_signature(payload, snap.get("signature",""))
        if not sig_valid:
            # try with config_hash included legacy mismatch: payload without config_hash was old style, try both
            payload_legacy = {k: v for k, v in snap.items() if k not in ("signature","published","published_at","published_by","rollback_from")}
            sig_valid = _verify_signature(payload_legacy, snap.get("signature",""))
    return {
        "tenant_id": tenant,
        "published_version": pub,
        "has_snapshot": snap is not None,
        "signature_valid": sig_valid,
        "config_hash": ch,
        "applied": applied,
        "applied_version": applied.get("applied_version") if isinstance(applied, dict) else None,
        "process_identity": f"{socket.gethostname()}:{os.getpid()}",
        "error": applied.get("error") if isinstance(applied, dict) else None,
    }

@router.get("/applied-status")
def get_applied_status_api(
    admin: AdminUser = Depends(get_current_admin),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    tenant_id: str | None = None,
):
    tenant = _resolve_tenant(tenant_id, x_tenant_id)
    pub = _published.get(tenant)
    try:
        db_pub, db_ch = _db_get_published_raw(tenant)
        if db_pub is not None:
            pub = db_pub
    except Exception:
        db_ch = None
    snap = get_published_snapshot(tenant) if pub is not None else None
    published_hash = None
    if snap is not None:
        published_hash = snap.get("config_hash") or _config_hash(snap.get("config",{}))
    applied = None
    try:
        applied = _db_fetch_applied(tenant)
    except Exception:
        applied = None
    # Also try to proxy to Control Plane live status if CP URL available (optional, non-blocking)
    cp_live = None
    try:
        import os as _os
        cp_url = _os.environ.get("OAOS_CP_BASE_URL") or _os.environ.get("OAOS_CONTROL_PLANE_URL") or "http://localhost:8100"
        # best-effort http fetch with short timeout, fail-soft
        import urllib.request, json as _j, socket as _s
        url = cp_url.rstrip("/") + f"/v1/runtime-config/status"
        req = urllib.request.Request(url, headers={"X-Tenant-Id": tenant, "X-User-Id": admin.email})
        with urllib.request.urlopen(req, timeout=0.7) as resp:  # type: ignore
            body = resp.read().decode("utf-8", errors="ignore")
            try:
                cp_live = _j.loads(body)
            except Exception:
                cp_live = {"raw": body[:500]}
    except Exception:
        cp_live = None
    return {
        "tenant_id": tenant,
        "published_version": pub,
        "published_hash": published_hash,
        "config_hash": published_hash,
        "applied": applied,
        "applied_version": applied.get("applied_version") if isinstance(applied, dict) else None,
        "applied_at": applied.get("applied_at") if isinstance(applied, dict) else None,
        "process_identity": applied.get("process_identity") if isinstance(applied, dict) else None,
        "error": applied.get("error") if isinstance(applied, dict) else None,
        "cp_live": cp_live,
    }

@router.get("/audit")
def get_audit_api(
    admin: AdminUser = Depends(require_l5),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    tenant_id: str | None = None,
    limit: int = 20,
):
    tenant = _resolve_tenant(tenant_id, x_tenant_id)
    items = [e for e in _audit_events if e.get("tenant_id") == tenant]
    if not items:
        # fallback: return last N regardless of tenant if none (global view for L5)
        items = _audit_events[-limit:]
    else:
        items = items[-limit:]
    return {"tenant_id": tenant, "count": len(items), "items": list(reversed(items))}

# ── internal endpoint for Control Plane (service-to-service) ─────────────────
# Shares same router but also mounted as /v1/internal/runtime-config via app.py shim
# Here expose a plain function for CP to call without HTTP:
def get_published_snapshot_internal(tenant_id: str = "default") -> dict | None:
    """CP calls this (or HTTP GET) to fetch canonical signed snapshot."""
    snap = get_published_snapshot(tenant_id)
    if snap is None:
        return None
    payload = {k: v for k, v in snap.items() if k not in ("signature","published","published_at","published_by","rollback_from")}
    if not _verify_signature(payload, snap.get("signature","")):
        if _is_production():
            raise RuntimeError("published snapshot signature invalid — fail-closed")
        # non-prod: return None to signal invalid
        return None
    return snap

def verify_snapshot_signature(snapshot: dict) -> bool:
    payload = {k: v for k, v in snapshot.items() if k not in ("signature","published","published_at","published_by","rollback_from")}
    return _verify_signature(payload, snapshot.get("signature",""))
