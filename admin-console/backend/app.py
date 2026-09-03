"""Open Agent OS — Admin API (admin-console/backend/app.py).

- FastAPI(title="Open Agent OS Admin API")
- auth router + infra router
- Section 22 dashboard proxy: stats (users/policy/audit counts, approvals pending)
- Section 23-24 approvals proxy, 30-31 audit proxy, credentials proxy
- CORS: whitelist via OAOS_CORS_ORIGINS (default localhost:3012,3000,8010,8100), deny * with credentials
"""
from __future__ import annotations

import os
import sys
import pathlib
import importlib.util
import types
import importlib.machinery

import logging

from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger(__name__)

def _ensure_admin_package():
    for pkg in ("admin_console", "admin_console.backend"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []  # type: ignore
            m.__spec__ = importlib.machinery.ModuleSpec(pkg, None, is_package=True)  # type: ignore
            sys.modules[pkg] = m

_ensure_admin_package()

def _load_admin_sibling(name: str):
    qname = f"admin_console.backend.{name}"
    if qname in sys.modules:
        return sys.modules[qname]
    # Robust aliasing: never trust bare 'auth'/'infra' collision from sys.path.
    # Only reuse bare if its __file__ points to admin-console/backend and has admin router.
    bare = sys.modules.get(name)
    if bare is not None and hasattr(bare, "router") and getattr(bare, "__file__", "") and "admin-console/backend" in getattr(bare, "__file__", ""):
        # also verify admin-specific attribute to avoid security.auth collision
        if hasattr(bare, "get_current_admin") or name != "auth":
            sys.modules[qname] = bare
            return bare
    p = pathlib.Path(__file__).parent / f"{name}.py"
    if not p.exists():
        raise ImportError(f"admin sibling not found: {p}")
    spec = importlib.util.spec_from_file_location(qname, str(p))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create spec for {qname}")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "admin_console.backend"
    sys.modules[qname] = mod
    spec.loader.exec_module(mod)  # type: ignore
    return mod

_auth_mod = _load_admin_sibling("auth")
auth_router = _auth_mod.router
get_current_admin = _auth_mod.get_current_admin
AdminUser = _auth_mod.AdminUser
_infra_mod = _load_admin_sibling("infra")
infra_router = _infra_mod.router
_business_mod = _load_admin_sibling("business")
business_router = _business_mod.router
_managed_mod = _load_admin_sibling("managed")
managed_router = _managed_mod.router
_user_mappings_mod = _load_admin_sibling("user_mappings")
user_mappings_router = _user_mappings_mod.router
_llm_providers_mod = _load_admin_sibling("llm_providers")
llm_providers_router = _llm_providers_mod.router
_runtime_mode_mod = _load_admin_sibling("runtime_mode")
runtime_mode_router = _runtime_mode_mod.router
_fallback_mod = _load_admin_sibling("fallback")
fallback_router = _fallback_mod.router

app = FastAPI(title="Open Agent OS Admin API", version="0.1.3")

# ── CORS — whitelist via OAOS_CORS_ORIGINS, deny * when credentials true ─
_DEFAULT_CORS_ORIGINS = [
    "http://localhost:3012",
    "http://localhost:3000",
    "http://localhost:8010",
    "http://localhost:8100",
]


def _get_cors_origins() -> list[str]:
    raw = os.environ.get("OAOS_CORS_ORIGINS", "")
    if not raw or not raw.strip():
        return list(_DEFAULT_CORS_ORIGINS)
    parts = [p.strip() for p in raw.split(",")]
    # filter empty, normalize
    origins = [p for p in parts if p]
    # SECURITY: never allow "*" together with allow_credentials=True
    if "*" in origins:
        logger.warning(
            "CORS: OAOS_CORS_ORIGINS contains '*', which is incompatible with allow_credentials=True — "
            "falling back to default whitelist (deny *)"
        )
        origins = [o for o in origins if o != "*"]
        if not origins:
            origins = list(_DEFAULT_CORS_ORIGINS)
    # dedupe preserve order
    seen: set[str] = set()
    deduped: list[str] = []
    for o in origins:
        if o not in seen:
            seen.add(o)
            deduped.append(o)
    return deduped


_CORS_ORIGINS = _get_cors_origins()

_CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
_CORS_ALLOW_HEADERS = [
    "Authorization",
    "Content-Type",
    "Accept",
    "Origin",
    "X-Requested-With",
    "X-User-Id",
    "X-Tenant-Id",
    "X-Agent-Id",
    "X-Groups",
    "X-Memory-Policy-Override",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=_CORS_ALLOW_METHODS,
    allow_headers=_CORS_ALLOW_HEADERS,
)

# ── Admin persistence (v1.6 §27.3) — oaos with in-memory fallback ─
try:
    _pers_mod = _load_admin_sibling("persistence")
    ensure_admin_tables = _pers_mod.ensure_admin_tables
    get_database_url = _pers_mod.get_database_url
except Exception:
    ensure_admin_tables = None  # type: ignore
    get_database_url = None  # type: ignore


@app.on_event("startup")
async def _admin_persistence_startup() -> None:
    """Startup hook — ensure admin tables if DB configured, else fallback.

    Fail-closed in production: if OAOS_ENV=production and no DATABASE_URL is
    configured, the underlying ensure_admin_tables() will raise and we do NOT
    swallow it — the app fails to start rather than silently running in-memory.

    In non-prod, falls back to in-memory and never raises.
    """
    if ensure_admin_tables is not None:
        is_prod = os.environ.get("OAOS_ENV", "").lower() == "production"
        if is_prod:
            # fail-closed: let RuntimeError propagate
            await ensure_admin_tables()
        else:
            try:
                await ensure_admin_tables()
            except Exception as exc:  # pragma: no cover - safety net
                logger.warning(f"Admin persistence startup fallback: {exc}")
    # Required log line per spec (exact substring match)
    logger.info("Admin persistence: oaos ready (or in-memory fallback)")


# ── Routers ──────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(infra_router)
app.include_router(business_router)
app.include_router(managed_router)
app.include_router(user_mappings_router)
app.include_router(llm_providers_router)
app.include_router(runtime_mode_router)
app.include_router(fallback_router)
# Setup wizard + ACP/MCP config routers (initial-install + runtime connection settings)
try:
    _setup_mod = _load_admin_sibling("setup")
    setup_router = _setup_mod.router
    app.include_router(setup_router)
    logger.info("Setup router mounted at /v1/setup")
except Exception as e:
    logger.warning(f"Setup router not mounted: {e}")
try:
    _acp_mod = _load_admin_sibling("acp_config")
    acp_router = _acp_mod.router
    app.include_router(acp_router)
    logger.info("ACP router mounted at /v1/acp")
except Exception as e:
    logger.warning(f"ACP router not mounted: {e}")
try:
    _mcp_mod = _load_admin_sibling("mcp_config")
    mcp_router = _mcp_mod.router
    app.include_router(mcp_router)
    logger.info("MCP router mounted at /v1/mcp")
except Exception as e:
    logger.warning(f"MCP router not mounted: {e}")
try:
    _mm_mod = _load_admin_sibling("mattermost_config")
    mm_router = _mm_mod.router
    app.include_router(mm_router)
    logger.info("Mattermost router mounted at /v1/mattermost")
except Exception as e:
    logger.warning(f"Mattermost router not mounted: {e}")
try:
    _ol_mod = _load_admin_sibling("outline_config")
    ol_router = _ol_mod.router
    app.include_router(ol_router)
    logger.info("Outline router mounted at /v1/outline")
except Exception as e:
    logger.warning(f"Outline router not mounted: {e}")
try:
    _notion_mod = _load_admin_sibling("notion_config")
    notion_router = _notion_mod.router
    app.include_router(notion_router)
    logger.info("Notion router mounted at /v1/notion")
except Exception as e:
    logger.warning(f"Notion router not mounted: {e}")
try:
    _slack_mod = _load_admin_sibling("slack_config")
    slack_router = _slack_mod.router
    app.include_router(slack_router)
    logger.info("Slack router mounted at /v1/slack")
except Exception as e:
    logger.warning(f"Slack router not mounted: {e}")
try:
    _oauth_mod = _load_admin_sibling("oauth_config")
    oauth_router = _oauth_mod.router
    app.include_router(oauth_router)
    logger.info("OAuth router mounted at /v1/oauth")
except Exception as e:
    logger.warning(f"OAuth router not mounted: {e}")
try:
    _smtp_mod = _load_admin_sibling("smtp_config")
    smtp_router = _smtp_mod.router
    app.include_router(smtp_router)
    logger.info("SMTP router mounted at /v1/smtp")
except Exception as e:
    logger.warning(f"SMTP router not mounted: {e}")
# P2 write surfaces — quota/embedding/secrets/feature-flags (additive; no enforcement/auth changes)
try:
    _quota_admin_mod = _load_admin_sibling("quota_admin")
    quota_admin_router = _quota_admin_mod.router
    app.include_router(quota_admin_router)
    logger.info("Quota admin router mounted at /v1/quota")
except Exception as e:
    logger.warning(f"Quota admin router not mounted: {e}")
try:
    _embedding_mod = _load_admin_sibling("embedding_config")
    embedding_router = _embedding_mod.router
    app.include_router(embedding_router)
    logger.info("Embedding router mounted at /v1/embedding")
except Exception as e:
    logger.warning(f"Embedding router not mounted: {e}")
try:
    _secrets_admin_mod = _load_admin_sibling("secrets_admin")
    secrets_admin_router = _secrets_admin_mod.router
    app.include_router(secrets_admin_router)
    logger.info("Secrets admin router mounted at /v1/secrets")
except Exception as e:
    logger.warning(f"Secrets admin router not mounted: {e}")
try:
    _flags_mod = _load_admin_sibling("feature_flags")
    flags_router = _flags_mod.router
    app.include_router(flags_router)
    logger.info("Feature flags router mounted at /v1/feature-flags")
except Exception as e:
    logger.warning(f"Feature flags router not mounted: {e}")
# P3 ops surfaces — profile/knowledge sync operation views (additive; no pipeline logic changes)
try:
    _profile_ops_mod = _load_admin_sibling("profile_ops")
    profile_ops_router = _profile_ops_mod.router
    app.include_router(profile_ops_router)
    logger.info("Profile ops router mounted at /v1/profile-ops")
except Exception as e:
    logger.warning(f"Profile ops router not mounted: {e}")
try:
    _knowledge_ops_mod = _load_admin_sibling("knowledge_ops")
    knowledge_ops_router = _knowledge_ops_mod.router
    app.include_router(knowledge_ops_router)
    logger.info("Knowledge ops router mounted at /v1/knowledge-ops")
except Exception as e:
    logger.warning(f"Knowledge ops router not mounted: {e}")
# Policy config router (Draft -> validation/simulation -> approval -> publish -> rollback)
try:
    _policy_mod = _load_admin_sibling("policy")
    policy_router = _policy_mod.router
    app.include_router(policy_router)
    logger.info("Policy router mounted at /v1/policy")
except Exception as _pe:
    logger.warning(f"Policy router not mounted: {_pe}")

# ── Runtime Configuration Plane Stage-1 (versioned/signed, fail-graceful) ──
try:
    _rc_mod = _load_admin_sibling("runtime_config")
    app.include_router(_rc_mod.router)
    logger.info("Runtime Config Plane router mounted at /v1/runtime/config")
except Exception as _rce:
    logger.warning(f"Runtime Config router not mounted: {_rce}")

# ── Personal Wiki (skeleton, lazy, fail-graceful) ──────────────────
try:
    _pw_mod = _load_admin_sibling("personal_wiki")
    personal_wiki_router = _pw_mod.router
    app.include_router(personal_wiki_router)
    logger.info("Personal Wiki router mounted at /v1/personal-wiki")
except Exception as e:
    logger.warning(f"Personal Wiki router not mounted: {e}")

# ── Personal Wiki consolidation scheduler (02:00 KST daily, fail gracefully) ─
try:
    _cons_path = pathlib.Path(__file__).resolve().parents[2] / "packages" / "personal-wiki" / "personal_wiki" / "consolidate.py"
    _pkg_root = pathlib.Path(__file__).resolve().parents[2] / "packages" / "personal-wiki"
    if str(_pkg_root) not in sys.path:
        sys.path.insert(0, str(_pkg_root))
    if _cons_path.exists():
        if "personal_wiki" not in sys.modules or not hasattr(sys.modules["personal_wiki"], "__path__"):
            _pkg = types.ModuleType("personal_wiki")
            _pkg.__path__ = [str(_cons_path.parent)]  # type: ignore
            _pkg.__spec__ = importlib.machinery.ModuleSpec("personal_wiki", None, is_package=True)  # type: ignore
            sys.modules["personal_wiki"] = _pkg
        spec = importlib.util.spec_from_file_location("personal_wiki.consolidate", str(_cons_path))
        if spec and spec.loader:
            if spec.name not in sys.modules:
                _cons_mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = _cons_mod
                spec.loader.exec_module(_cons_mod)  # type: ignore
            else:
                _cons_mod = sys.modules[spec.name]
            register_consolidation_scheduler = getattr(_cons_mod, "register_consolidation_scheduler", None)
            if register_consolidation_scheduler and os.environ.get("OAOS_WIKI_CONSOLIDATION_CRON", "0") in ("1", "true", "True"):
                try:
                    _sched_res = register_consolidation_scheduler()
                    logger.info(f"Wiki consolidation scheduler: {_sched_res}")
                except Exception as _se:
                    logger.warning(f"Wiki consolidation scheduler not registered: {_se}")
            else:
                logger.info("Wiki consolidation scheduler idle (set OAOS_WIKI_CONSOLIDATION_CRON=1 to enable APScheduler 02:00 KST)")
        else:
            logger.warning("Wiki consolidation scheduler import skipped: spec failed")
    else:
        logger.warning("Wiki consolidation scheduler import skipped: consolidate.py missing")
except Exception as _e:
    logger.warning(f"Wiki consolidation scheduler import skipped: {_e}")


# ── Health ───────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "admin-api"}


# ── helpers to reach security stores ─────────────────────────────
def _get_security():
    """Try to import security.app and return (approval_store, audit_ledger, delegation_service) tuple."""
    try:
        import security.app as sec  # type: ignore

        return (
            getattr(sec, "approval_store", None),
            getattr(sec, "audit_ledger", None),
            getattr(sec, "delegation_service", None),
        )
    except Exception:
        return None, None, None


def _serialize_approval(v) -> dict:
    """Best-effort serialize ApprovalRequest."""
    try:
        return v.model_dump(mode="json")  # type: ignore
    except Exception:
        # fallback manual
        return {
            "approval_id": getattr(v, "approval_id", getattr(v, "id", "")),
            "user_id": getattr(v, "user_id", ""),
            "agent_id": getattr(v, "agent_id", ""),
            "action": getattr(v, "action", ""),
            "resource": getattr(v, "resource", ""),
            "risk": getattr(v, "risk", ""),
            "expires_at": str(getattr(v, "expires_at", "")),
            "decision": str(getattr(v, "decision", "PENDING")),
            "status": str(getattr(v, "decision", getattr(v, "status", "PENDING"))),
            "request_hash": getattr(v, "request_hash", None),
            "nonce": getattr(v, "nonce", None),
            "signature": getattr(v, "signature", None),
            "decided_at": str(getattr(v, "decided_at", "")) if getattr(v, "decided_at", None) else None,
            "decided_by": getattr(v, "decided_by", None),
        }


def _is_pending(v) -> bool:
    dec = getattr(v, "decision", getattr(v, "status", ""))
    # enum or string
    try:
        val = dec.value if hasattr(dec, "value") else str(dec)
    except Exception:
        val = str(dec)
    return val.upper() == "PENDING"


# ── Dashboard proxy (Section 22) ─────────────────────────────────
@app.get("/v1/dashboard/stats")
def dashboard_stats(admin: AdminUser = Depends(get_current_admin)):
    """Dashboard stats proxy — aggregates counts from Security & Governance.

    Tries to import live security stores; falls back to admin-local counts.
    """
    # default fallback
    users_count = 0
    policy_count = 0
    audit_count = 0
    pending_approvals: list = []

    # try to collect from security app stores (if available via import)
    try:
        import importlib.util
        from pathlib import Path

        sec_app_path = Path(__file__).resolve().parents[2] / "security" / "app.py"
        if sec_app_path.exists():
            pass
    except Exception:
        pass

    # admin-local counts (always available) — isolated, no bare auth collision
    try:
        users_count = len(_auth_mod.list_users())
    except Exception:
        try:
            users_count = len(sys.modules["admin_console.backend.auth"].list_users())  # type: ignore
        except Exception:
            pass

    infra_count = 0
    # Prefer DB count (authoritative) over in-memory _services — isolated, no bare infra
    try:
        _infra_services = _infra_mod._services  # type: ignore
        # Try DB count first (persistent)
        db_count = None
        try:
            import os
            url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
            if url and "oaos" in url:
                from sqlalchemy import create_engine, text
                sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://").replace("postgresql://", "postgresql+psycopg://") if url.startswith("postgresql") else url
                # strip +asyncpg fallback already handled
                if "+asyncpg" in sync_url:
                    sync_url = sync_url.replace("+asyncpg", "+psycopg")
                eng = create_engine(sync_url, pool_pre_ping=False)
                with eng.connect() as conn:
                    row = conn.execute(text("SELECT COUNT(*) FROM admin_infra_services")).fetchone()
                    if row is not None:
                        db_count = int(row[0])
                eng.dispose()
        except Exception:
            db_count = None
        if db_count is not None:
            infra_count = db_count
        else:
            infra_count = len(_infra_services)
    except Exception:
        infra_count = 0

    try:
        from delegation.delegation_service.service import DelegationService  # type: ignore
        pass
    except Exception:
        pass

    # Attempt to read security app globals if already imported
    try:
        import security.app as sec  # type: ignore

        ds = getattr(sec, "delegation_service", None)
        if ds is not None:
            if hasattr(ds, "_delegations"):
                users_count = max(users_count, len(getattr(ds, "_delegations", {})))
            elif hasattr(ds, "_store"):
                users_count = max(users_count, len(getattr(ds, "_store", {})))
    except Exception:
        pass
    try:
        import security.app as sec  # type: ignore

        al = getattr(sec, "audit_ledger", None)
        if al is not None:
            audit_count = al.count
    except Exception:
        pass
    try:
        import security.app as sec  # type: ignore

        pe = getattr(sec, "policy_engine", None)
        if pe is not None:
            bundles = getattr(pe, "bundles", [])
            policy_count = len(bundles)
    except Exception:
        pass
    try:
        import security.app as sec  # type: ignore

        aps = getattr(sec, "approval_store", None)
        if aps is not None:
            if hasattr(aps, "_requests"):
                store = getattr(aps, "_requests", {})
                pending_approvals = [v for v in store.values() if _is_pending(v)]
            elif hasattr(aps, "_store"):
                store = getattr(aps, "_store", {})
                pending_approvals = [v for v in store.values() if _is_pending(v)]
            elif hasattr(aps, "list_pending"):
                pending_approvals = aps.list_pending()  # type: ignore
    except Exception:
        pass

    pending_n = len(pending_approvals) if isinstance(pending_approvals, list) else 0
    # Frontend compat keys (DashboardPage expects these) + legacy keys
    return {
        "users_count": users_count,
        "policy_count": policy_count,
        "audit_count": audit_count,
        "infra_services_count": infra_count,
        "pending_approvals_count": pending_n,
        # Frontend DashboardPage compat (aliases)
        "total_users": users_count,
        "total_agents": users_count,  # logical personal agents 1:1 with users (§14)
        "pending_approvals": pending_n,
        "audit_events_today": audit_count,
    }


def _list_pending_approvals() -> list[dict]:
    pending = []
    aps, _, _ = _get_security()
    if aps is not None:
        try:
            if hasattr(aps, "_requests"):
                store = getattr(aps, "_requests", {})
                for v in store.values():
                    if _is_pending(v):
                        pending.append(_serialize_approval(v))
            elif hasattr(aps, "_store"):
                store = getattr(aps, "_store", {})
                for v in store.values():
                    if _is_pending(v):
                        pending.append(_serialize_approval(v))
            elif hasattr(aps, "list_pending"):
                raw = aps.list_pending()  # type: ignore
                for v in raw:
                    pending.append(_serialize_approval(v))
        except Exception:
            pass
    return pending


@app.get("/v1/dashboard/approvals")
def dashboard_approvals(admin: AdminUser = Depends(get_current_admin)):
    """Pending approvals list proxy (legacy dashboard path)."""
    pending = _list_pending_approvals()
    return {"pending": pending, "count": len(pending)}


# ── Approvals proxy (Section 23-24) ──────────────────────────────
@app.get("/v1/approvals")
def approvals_list(limit: int = 5, admin: AdminUser = Depends(get_current_admin)):
    """GET /v1/approvals — frontend compat (limit=5)."""
    pending = _list_pending_approvals()
    items = pending[: max(1, min(50, limit))]
    return {"items": items, "count": len(pending)}

@app.get("/v1/approvals/pending")
def approvals_pending(admin: AdminUser = Depends(get_current_admin)):
    """GET /v1/approvals/pending — pending approvals list."""
    pending = _list_pending_approvals()
    return {"pending": pending, "count": len(pending)}


class ApprovalDecideRequest(BaseModel):
    approval_id: str
    decision: str
    decided_by: str | None = None
    group_id: str | None = None


@app.post("/v1/approvals/decide")
def approvals_decide(body: ApprovalDecideRequest, admin: AdminUser = Depends(get_current_admin)):
    """POST /v1/approvals/decide — decide approval (4 decisions)."""
    aps, _, _ = _get_security()
    if aps is None:
        raise HTTPException(status_code=503, detail="approval_store not available")

    # map decision string to enum
    decision_str = body.decision.upper()
    # normalize: allow DENIED / APPROVED_ONCE etc
    allowed = {"DENIED", "APPROVED_ONCE", "APPROVED_USER_ALWAYS", "APPROVED_GROUP_ALWAYS", "PENDING"}
    if decision_str not in allowed:
        raise HTTPException(status_code=400, detail=f"decision must be one of {allowed}")

    # need enum instance if approval_store expects ApprovalDecision
    decision_val = decision_str
    try:
        from approval.approval_workflow.workflow import ApprovalDecision  # type: ignore

        # try to get enum member
        try:
            decision_val = ApprovalDecision(decision_str)  # type: ignore
        except Exception:
            decision_val = ApprovalDecision[decision_str]  # type: ignore
    except Exception:
        pass

    decided_by = body.decided_by or admin.email

    try:
        result = aps.decide(
            approval_id=body.approval_id,
            decision=decision_val,  # type: ignore
            decided_by=decided_by,
            group_id=body.group_id,
        )
        # audit event for decision (if ledger available)
        try:
            _, al, _ = _get_security()
            if al is not None:
                from audit_model.model import AuditEvent, AuditEventType  # type: ignore
                import uuid

                evt = AuditEvent(
                    event_id=f"evt_{uuid.uuid4().hex[:12]}",
                    event_type=AuditEventType.APPROVAL_DECISION,
                    timestamp=datetime.now(timezone.utc),
                    tenant_id="default",
                    user_id=getattr(result, "user_id", None),
                    agent_id=getattr(result, "agent_id", None),
                    resource=getattr(result, "resource", None),
                    action=getattr(result, "action", None),
                    decision=str(decision_str),
                )
                al.append(evt)
        except Exception:
            pass
        return _serialize_approval(result)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Audit proxy (Section 30-31) ──────────────────────────────────
@app.get("/v1/audit/events")
def audit_events(admin: AdminUser = Depends(get_current_admin)):
    """GET /v1/audit/events — audit ledger events."""
    _, al, _ = _get_security()
    if al is None:
        return {"events": [], "count": 0, "head": None}
    try:
        events = [e.model_dump(mode="json") for e in al.events]
        return {"events": events, "count": al.count, "head": al.head}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/audit/chain")
def audit_chain(admin: AdminUser = Depends(get_current_admin)):
    """GET /v1/audit/chain — frontend compat (AuditChain {head_hash, chain_length, verified})."""
    _, _, al = _get_security()
    chain_length = 0
    head_hash = None
    verified = True
    try:
        if al is not None:
            if hasattr(al, "verify_chain"):
                vr = al.verify_chain()
                verified = bool(vr) if not isinstance(vr, dict) else bool(vr.get("valid", True))
            if hasattr(al, "head"):
                h = al.head()
                head_hash = getattr(h, "hash", None) if h is not None else None
                if isinstance(h, dict):
                    head_hash = h.get("hash") or h.get("head_hash")
            if hasattr(al, "count"):
                chain_length = int(al.count()) if callable(getattr(al, "count")) else int(getattr(al, "count", 0))
            elif hasattr(al, "_events"):
                chain_length = len(getattr(al, "_events", []))
            elif hasattr(al, "events"):
                ev = getattr(al, "events")
                chain_length = len(ev) if isinstance(ev, list) else 0
    except Exception:
        pass
    return {"head_hash": head_hash, "chain_length": chain_length, "verified": verified, "last_checkpoint": None}

@app.get("/v1/audit/verify")
def audit_verify(admin: AdminUser = Depends(get_current_admin)):
    """GET /v1/audit/verify — hash-chain + checkpoint verification."""
    _, al, _ = _get_security()
    if al is None:
        return {"chain_valid": True, "checkpoint_valid": True, "event_count": 0, "head": None}
    try:
        chain_valid = al.verify_chain()
        event_count = al.count
        head = al.head
        # checkpoint verification
        checkpoint_valid: bool | None = None
        checkpoint = None
        try:
            cp = al.checkpoint()
            checkpoint = cp.model_dump(mode="json") if hasattr(cp, "model_dump") else dict(cp)
            checkpoint_valid = al.verify_checkpoint(cp)
        except Exception:
            checkpoint_valid = None
        result: dict = {
            "chain_valid": chain_valid,
            "event_count": event_count,
            "head": head,
            "checkpoint": checkpoint,
        }
        if checkpoint_valid is not None:
            result["checkpoint_valid"] = checkpoint_valid
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/audit/checkpoint")
def audit_checkpoint(admin: AdminUser = Depends(get_current_admin)):
    """GET /v1/audit/checkpoint — current checkpoint (head hash + signature)."""
    _, al, _ = _get_security()
    if al is None:
        return {"chain_head_hash": "", "event_count": 0, "created_at": None, "signature": ""}
    try:
        cp = al.checkpoint()
        if hasattr(cp, "model_dump"):
            return cp.model_dump(mode="json")
        return dict(cp)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Policy bundles proxy (Section 25) ──────────────────────────
# Delegates to policy module (active published bundle is authoritative). Kept here for backwards compat
# if policy router failed to mount; otherwise policy router handles /v1/policy/bundles.
try:
    _policy_mod_legacy = sys.modules.get("admin_console.backend.policy") or _load_admin_sibling("policy")
    _legacy_active = getattr(_policy_mod_legacy, "get_active_published_bundle", None)
    _legacy_draft = getattr(_policy_mod_legacy, "get_draft_bundle", None)
except Exception:
    _legacy_active = None
    _legacy_draft = None

@app.get("/v1/policy/bundles")
def policy_bundles(admin: AdminUser = Depends(get_current_admin)):
    """Return Policy Bundles — active published bundle is authoritative (policy module). Fallback to default_bundle if none published."""
    # Prefer policy module's DB-published bundle when available (authoritative per spec).
    try:
        if _legacy_active is not None:
            active = _legacy_active("default")
            if active is not None:
                try:
                    from pathlib import Path as _P
                    import sys as _sys2
                    for _p in [str(_P(__file__).resolve().parents[2] / "packages" / "policy-model"), str(_P(__file__).resolve().parents[2] / "security" / "policy-engine")]:
                        if _p not in _sys2.path:
                            _sys2.path.insert(0, _p)
                    from policy_model import POLICY_EVALUATION_ORDER  # type: ignore
                    order = [s.value for s in POLICY_EVALUATION_ORDER]
                except Exception:
                    order = ["explicit_deny","security_boundary_deny","personal_delegation","persistent_user_grant","group_grant","default_bundle","jit_approval","default_deny"]
                bundle = {"id": active.get("bundle_id") or "default-bundle-v1", "tenant_id": active.get("tenant_id") or "default", "name": active.get("name") or "Default Policy Bundle", "version": active.get("version"), "rules": active.get("rules") or []}
                return {"bundles": [bundle], "evaluation_order": order, "draft": _legacy_draft("default") if _legacy_draft else None, "active_version": active.get("version")}
    except Exception:
        pass
    bundles_data: list[dict] = []
    policy_order: list[str] = []
    try:
        import sys as _sys2
        from pathlib import Path as _P
        for _p in [str(_P(__file__).resolve().parents[2] / "packages" / "policy-model"), str(_P(__file__).resolve().parents[2] / "security" / "policy-engine")]:
            if _p not in _sys2.path:
                _sys2.path.insert(0, _p)
        from policy_model import POLICY_EVALUATION_ORDER  # type: ignore
        policy_order = [s.value for s in POLICY_EVALUATION_ORDER]
    except Exception:
        policy_order = ["explicit_deny","security_boundary_deny","personal_delegation","persistent_user_grant","group_grant","default_bundle","jit_approval","default_deny"]
    try:
        import security.app as sec  # type: ignore
        pe = getattr(sec, "policy_engine", None)
        if pe is not None:
            for b in getattr(pe, "bundles", []):
                try:
                    bundles_data.append(b.model_dump(mode="json"))
                except Exception:
                    bundles_data.append({"id": getattr(b,"id",""),"tenant_id": getattr(b,"tenant_id",""),"version": getattr(b,"version",""),"rules": []})
    except Exception:
        pass
    if not bundles_data:
        try:
            from policy_engine.default_bundle import default_bundle as _db  # type: ignore
            b = _db(tenant_id="default")
            bundles_data = [b.model_dump(mode="json")]
        except Exception:
            pass
    return {"bundles": bundles_data, "evaluation_order": policy_order}


# ── Credentials proxy (Section 22) ───────────────────────────────
@app.get("/v1/credentials/status")
def credentials_status(admin: AdminUser = Depends(get_current_admin)):
    """GET /v1/credentials/status — provider별 active/revoked counts + recent delegations."""
    _, _, ds = _get_security()
    if ds is None:
        return {
            "providers": [],
            "total": 0,
            "active": 0,
            "revoked": 0,
            "expired": 0,
            "recent": [],
        }
    try:
        store: dict = getattr(ds, "_store", {})
        bindings: dict = getattr(ds, "_bindings", {})
        # aggregate per provider
        from collections import Counter, defaultdict

        provider_stats: dict[str, dict] = defaultdict(lambda: {"provider": "", "total": 0, "active": 0, "revoked": 0, "expired": 0})
        total = 0
        active = 0
        revoked = 0
        expired = 0

        for d in store.values():
            prov = getattr(d, "provider", "unknown")
            status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", ""))).upper()
            if prov not in provider_stats:
                provider_stats[prov] = {"provider": prov, "total": 0, "active": 0, "revoked": 0, "expired": 0}
            else:
                provider_stats[prov]["provider"] = prov
            provider_stats[prov]["total"] += 1
            total += 1
            if status == "ACTIVE":
                # also check expiry
                exp = getattr(d, "expires_at", None)
                if exp is not None:
                    try:
                        # exp may be datetime or string
                        from datetime import datetime as _dt

                        if isinstance(exp, str):
                            exp_dt = _dt.fromisoformat(exp.replace("Z", "+00:00"))
                        else:
                            exp_dt = exp
                        if exp_dt < datetime.now(timezone.utc):
                            provider_stats[prov]["expired"] += 1
                            expired += 1
                        else:
                            provider_stats[prov]["active"] += 1
                            active += 1
                    except Exception:
                        provider_stats[prov]["active"] += 1
                        active += 1
                else:
                    provider_stats[prov]["active"] += 1
                    active += 1
            elif status == "REVOKED":
                provider_stats[prov]["revoked"] += 1
                revoked += 1
            elif status == "EXPIRED":
                provider_stats[prov]["expired"] += 1
                expired += 1
            else:
                # treat unknown as active
                provider_stats[prov]["active"] += 1
                active += 1

        # recent delegations — last 10 by created_at desc
        def _created_at(d):
            c = getattr(d, "created_at", None)
            if c is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            if isinstance(c, str):
                try:
                    return datetime.fromisoformat(c.replace("Z", "+00:00"))
                except Exception:
                    return datetime.min.replace(tzinfo=timezone.utc)
            return c

        sorted_delegations = sorted(store.values(), key=_created_at, reverse=True)[:10]
        recent = []
        for d in sorted_delegations:
            try:
                recent.append(d.model_dump(mode="json"))  # type: ignore
            except Exception:
                recent.append(
                    {
                        "id": getattr(d, "id", ""),
                        "user_id": getattr(d, "user_id", ""),
                        "agent_id": getattr(d, "agent_id", ""),
                        "provider": getattr(d, "provider", ""),
                        "scope": getattr(d, "scope", ""),
                        "status": str(getattr(d, "status", "")),
                        "created_at": str(getattr(d, "created_at", "")),
                    }
                )

        # include binding counts per provider as extra
        binding_counts: dict[str, int] = Counter()
        for b in bindings.values():
            prov = getattr(b, "provider", "unknown")
            binding_counts[prov] += 1
        for prov, cnt in binding_counts.items():
            if prov in provider_stats:
                provider_stats[prov]["bindings"] = cnt
            else:
                provider_stats[prov] = {"provider": prov, "total": 0, "active": 0, "revoked": 0, "expired": 0, "bindings": cnt}

        providers_list = sorted(provider_stats.values(), key=lambda x: x["provider"])

        return {
            "providers": providers_list,
            "total": total,
            "active": active,
            "revoked": revoked,
            "expired": expired,
            "recent": recent,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
