"""Active published policy loader — shared read-only bridge between admin policy storage and MattermostPolicyGate.

- Reads admin_policy_versions table (status='published') if DB configured (OAOS_DATABASE_URL/DATABASE_URL).
- Read-only SELECT only; never creates tables or mutates data.
- Falls back to admin in-memory store (non-prod) via lazy import of admin_console.backend.policy if DB not configured.
- Converts stored rule dicts -> PolicyBundle (deterministic, fail-safe).
- No circular imports: admin backend never imports this module; this module optionally imports admin backend only for in-memory fallback.
"""
from __future__ import annotations

import json
import os
import sys
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

def _is_prod() -> bool:
    return os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod")

def _db_sync_url() -> Optional[str]:
    # Stage 3: support OAOS_CP_DATABASE_URL (control-plane systemd) + repo .env fallback
    url = (
        os.environ.get("OAOS_DATABASE_URL")
        or os.environ.get("OAOS_CP_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not url or not url.strip():
        # Fallback: read repo .env (systemd EnvironmentFile missing case) — safe read-only
        try:
            _repo_env = Path(__file__).resolve().parents[3] / ".env"
            if _repo_env.exists():
                for _line in _repo_env.read_text(encoding="utf-8").splitlines():
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _sep, _v = _line.partition("=")
                    if _k.strip() in ("OAOS_DATABASE_URL", "DATABASE_URL", "OAOS_CP_DATABASE_URL"):
                        _cand = _v.strip().strip('"').strip("'")
                        if _cand:
                            url = _cand
                            break
        except Exception:
            pass
        if not url or not url.strip():
            return None
    u = url.strip()
    if u.startswith("postgresql+asyncpg://"):
        u = u.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    elif "+asyncpg" in u:
        u = u.replace("+asyncpg", "+psycopg")
    elif u.startswith("postgresql://"):
        u = u.replace("postgresql://", "postgresql+psycopg://", 1)
    if "+aiosqlite" in u:
        u = u.replace("+aiosqlite", "")
        u = u.replace("sqlite+://", "sqlite://")
    if u.startswith("sqlite+"):
        u = u.replace("sqlite+", "sqlite", 1)
    if u.startswith("sqlite+aiosqlite://"):
        u = u.replace("sqlite+aiosqlite://", "sqlite://", 1)
    return u

def _get_db_active_dict(tenant_id: str = "default") -> Optional[dict]:
    # In non-prod, respect admin persistence's get_database_url (env-only, not .env) so
    # loader and admin policy agree on DB vs in-memory store. If admin will write to
    # mem (because env has no DATABASE_URL), loader must read from mem, not stale DB
    # found via repo .env fallback.
    if not _is_prod():
        try:
            # try canonical persistence helper if available
            for mod_name in ("persistence", "admin_console.backend.persistence"):
                m = sys.modules.get(mod_name)
                if m is not None and hasattr(m, "get_database_url"):
                    try:
                        if not m.get_database_url():  # type: ignore
                            # also allow OAOS_CP_DATABASE_URL (control-plane systemd) as authoritative DB URL
                            if not os.environ.get("OAOS_CP_DATABASE_URL"):
                                return None
                    except Exception:
                        pass
                    break
            else:
                # Fallback direct env check. OAOS_CP_DATABASE_URL is a
                # control-plane-owned database and must be honored even when
                # the admin persistence module is not imported yet.
                if not (
                    os.environ.get("OAOS_DATABASE_URL")
                    or os.environ.get("OAOS_CP_DATABASE_URL")
                    or os.environ.get("DATABASE_URL")
                ):
                    return None
        except Exception:
            pass
    url = _db_sync_url()
    if not url:
        return None
    try:
        from sqlalchemy import create_engine, text  # type: ignore
    except Exception as e:
        if _is_prod():
            raise RuntimeError(f"active_policy_loader: sqlalchemy not available in production: {e}") from e
        logger.debug("active_policy_loader: sqlalchemy not available")
        return None
    engine = None
    try:
        connect_args = {}
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        engine = create_engine(url, echo=False, pool_pre_ping=False, connect_args=connect_args)
        with engine.connect() as conn:
            # check table exists quickly — in prod, missing table is fail-closed
            try:
                row = conn.execute(text(
                    "SELECT id, tenant_id, bundle_id, name, version, status, rules_json, created_by, created_at, approved_by, approved_at, published_at, parent_version "
                    "FROM admin_policy_versions WHERE tenant_id=:t AND status='published' ORDER BY published_at DESC, created_at DESC, version DESC LIMIT 1"
                ), {"t": tenant_id}).mappings().first()
            except Exception as e:
                if _is_prod():
                    raise RuntimeError(f"active_policy_loader DB query failed in production: {e}") from e
                logger.debug(f"active_policy_loader DB query failed (table missing?): {e}")
                return None
            if row is None:
                return None
            raw = dict(row)
            try:
                rules = json.loads(raw.get("rules_json") or "[]") if isinstance(raw.get("rules_json"), str) else (raw.get("rules_json") or [])
            except Exception:
                rules = []
            return {
                "id": raw.get("id"),
                "tenant_id": raw.get("tenant_id") or tenant_id,
                "bundle_id": raw.get("bundle_id") or "default-bundle-v1",
                "name": raw.get("name") or "Default Policy Bundle",
                "version": raw.get("version") or "1.0.0",
                "status": raw.get("status"),
                "rules": rules,
                "rules_json": raw.get("rules_json"),
                "created_by": raw.get("created_by"),
                "created_at": str(raw.get("created_at")) if raw.get("created_at") else None,
                "approved_by": raw.get("approved_by"),
                "approved_at": str(raw.get("approved_at")) if raw.get("approved_at") else None,
                "published_at": str(raw.get("published_at")) if raw.get("published_at") else None,
                "parent_version": raw.get("parent_version"),
            }
    except RuntimeError:
        raise
    except Exception as e:
        if _is_prod():
            raise RuntimeError(f"active_policy_loader DB error in production: {e}") from e
        logger.debug(f"active_policy_loader DB error: {e}")
        return None
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass

def _get_mem_active_dict(tenant_id: str = "default") -> Optional[dict]:
    """Try to read from admin in-memory fallback (non-prod tests). Read-only."""
    # Try explicit import paths first (canonical)
    for mod_name in ("admin_console.backend.policy", "policy"):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "get_active_published_bundle"):
            try:
                rec = mod.get_active_published_bundle(tenant_id)  # type: ignore
                if rec is not None:
                    return rec
            except Exception:
                pass
    # Robust scan: tests load admin policy via importlib spec with ad-hoc names
    # e.g. admin_policy_mod, admin_app_policy, admin_policy_mod etc.
    # Any module exposing _mem_versions / _db_get_active_published holds the published state.
    for mod in list(sys.modules.values()):
        if mod is None:
            continue
        # 1) direct get_active_published_bundle wrapper
        if hasattr(mod, "get_active_published_bundle") and hasattr(mod, "_mem_versions"):
            try:
                rec = mod.get_active_published_bundle(tenant_id)  # type: ignore
                if rec is not None:
                    return rec
            except Exception:
                pass
        # 2) legacy internal helper _db_get_active_published
        if hasattr(mod, "_db_get_active_published"):
            try:
                rec = mod._db_get_active_published(tenant_id)  # type: ignore
                if rec is not None:
                    return rec
            except Exception:
                pass
        # 3) raw _mem_versions list scan (process-local in-memory fallback)
        if hasattr(mod, "_mem_versions"):
            try:
                versions = getattr(mod, "_mem_versions")
                if isinstance(versions, list) and versions:
                    # filter published for this tenant
                    published = [v for v in versions if isinstance(v, dict) and v.get("tenant_id") == tenant_id and v.get("status") == "published"]
                    if published:
                        def _key(v):
                            return (v.get("published_at") or v.get("created_at") or "", v.get("version", ""))
                        published.sort(key=_key)
                        return published[-1]
            except Exception:
                pass
    # Try lazy file load if not yet imported but file exists
    try:
        import importlib.util as _ilu
        import pathlib as _pl
        p = _pl.Path(__file__).resolve().parents[3] / "admin-console" / "backend" / "policy.py"
        if p.exists():
            # only load if not already in sys.modules to avoid double exec
            spec = _ilu.spec_from_file_location("_active_policy_mem_probe", str(p))
            if spec and spec.loader:
                # we don't exec full module (would duplicate state); instead try to import via spec already?
                # fallback: attempt import via importlib
                import importlib as _il
                try:
                    m = _il.import_module("admin_console.backend.policy")
                    if hasattr(m, "get_active_published_bundle"):
                        rec = m.get_active_published_bundle(tenant_id)  # type: ignore
                        if rec is not None:
                            return rec
                except Exception:
                    pass
    except Exception:
        pass
    return None

def get_active_published_dict(tenant_id: str = "default") -> Optional[dict]:
    """Shared read-only accessor. Returns dict with keys bundle_id, version, rules etc or None if no published."""
    # DB first (authoritative in prod) — may raise in production
    rec = _get_db_active_dict(tenant_id)
    if rec is not None:
        return rec
    # In-memory fallback — non-prod only; in prod skip to keep fail-closed semantics
    if _is_prod():
        # if DB was configured but returned None due to no rows, that's legitimate fallback to default bundle;
        # but we already tried DB and got None without exception, so no error — allow fallback.
        # We only skip mem fallback that could hide DB misconfig; DB None+no rows => fallback to small_business.
        # For safety, still allow mem fallback only if no DB URL (i.e., _db_sync_url is None)
        if _db_sync_url() is not None:
            return None
    try:
        return _get_mem_active_dict(tenant_id)
    except Exception:
        return None

def _dict_to_bundle(rec: dict):
    """Convert admin dict -> PolicyBundle. Fail-safe: skips invalid rules."""
    # lazy imports to keep policy-engine install light
    ROOT = Path(__file__).resolve().parents[2]
    # ensure packages on path
    for p in [ROOT / "../../packages/policy-model"]:
        if str(p.resolve()) not in sys.path:
            pass
    try:
        from policy_model import PolicyBundle, PolicyRule, PolicyDecision, PolicySource  # type: ignore
    except Exception:
        # try alternative import path
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "policy-model"))
        from policy_model import PolicyBundle, PolicyRule, PolicyDecision, PolicySource  # type: ignore

    rules_in = rec.get("rules") or []
    out_rules = []
    for r in rules_in:
        try:
            rid = str(r.get("id") or "").strip()
            if not rid:
                continue
            src_raw = str(r.get("source") or "default_bundle").strip()
            eff_raw = str(r.get("effect") or "DENY").strip().upper()
            act = str(r.get("action") or "*").strip() or "*"
            pat = str(r.get("resource_pattern") or "*").strip() or "*"
            pri = int(r.get("priority") or 0)
            desc = r.get("description")
            # map source/effect strings to enums (case-insensitive)
            try:
                source = PolicySource(src_raw)
            except Exception:
                # try lowercased lookup
                source = PolicySource(src_raw.lower())
            try:
                effect = PolicyDecision(eff_raw)
            except Exception:
                effect = PolicyDecision.DENY if eff_raw == "DENY" else (PolicyDecision.ALLOW if eff_raw == "ALLOW" else PolicyDecision.APPROVAL_REQUIRED)
            out_rules.append(PolicyRule(id=rid, source=source, action=act, resource_pattern=pat, effect=effect, priority=pri, description=desc))
        except Exception as e:
            logger.debug(f"active_policy_loader skip invalid rule {r}: {e}")
            continue
    # If no valid rules, treat as no bundle (fail to fallback)
    if not out_rules:
        return None
    tid = rec.get("tenant_id") or "default"
    return PolicyBundle(
        id=rec.get("bundle_id") or rec.get("id") or "active-published-bundle",
        tenant_id=tid,
        name=rec.get("name") or "Active Published Policy",
        version=rec.get("version") or "1.0.0",
        rules=out_rules,
    )

def get_active_published_bundle(tenant_id: str = "default"):
    """Return PolicyBundle for tenant if published exists, else None. Read-only."""
    rec = get_active_published_dict(tenant_id)
    if rec is None:
        return None
    try:
        b = _dict_to_bundle(rec)
        return b
    except Exception as e:
        logger.debug(f"active_policy_loader bundle conversion failed: {e}")
        return None

# Convenience alias for control-plane: returns PolicyEngine if active exists
def get_active_policy_engine(tenant_id: str = "default"):
    """Return PolicyEngine wrapping active published bundle if present, else None."""
    bundle = get_active_published_bundle(tenant_id)
    if bundle is None:
        return None
    try:
        # ensure engine importable
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from policy_engine.engine import PolicyEngine  # type: ignore
        return PolicyEngine([bundle])
    except Exception:
        try:
            from policy_engine.engine import PolicyEngine  # type: ignore
            return PolicyEngine([bundle])
        except Exception as e:
            logger.debug(f"active_policy_loader engine build failed: {e}")
            return None
