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

# Ensure sibling imports work when run as module or via pytest conftest SYS.PATH injection
sys.path.insert(0, os.path.dirname(__file__))

import logging

from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger(__name__)

try:
    from auth import router as auth_router, get_current_admin, AdminUser  # type: ignore
    from infra import router as infra_router  # type: ignore
    from business import router as business_router  # type: ignore
    from managed import router as managed_router  # type: ignore
    from user_mappings import router as user_mappings_router  # type: ignore
except ImportError:
    from .auth import router as auth_router, get_current_admin, AdminUser  # type: ignore
    from .infra import router as infra_router  # type: ignore
    from .business import router as business_router  # type: ignore
    from .managed import router as managed_router  # type: ignore
    from .user_mappings import router as user_mappings_router  # type: ignore

app = FastAPI(title="Open Agent OS Admin API", version="0.1.1")

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

# ── Admin persistence (v1.6 §27.3) — openagentos with in-memory fallback ─
try:
    from persistence import ensure_admin_tables, get_database_url  # type: ignore
except ImportError:
    try:
        from .persistence import ensure_admin_tables, get_database_url  # type: ignore
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
    logger.info("Admin persistence: openagentos ready (or in-memory fallback)")


# ── Routers ──────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(infra_router)
app.include_router(business_router)
app.include_router(managed_router)
app.include_router(user_mappings_router)


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

    # admin-local counts (always available)
    try:
        from auth import list_users

        users_count = len(list_users())
    except Exception:
        pass

    infra_count = 0
    # Prefer DB count (authoritative) over in-memory _services
    try:
        from infra import _services as _infra_services
        # Try DB count first (persistent)
        db_count = None
        try:
            import os
            url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
            if url and "openagentos" in url:
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
@app.get("/v1/policy/bundles")
def policy_bundles(admin: AdminUser = Depends(get_current_admin)):
    """Return Policy Bundles from security policy_engine (importlib, fallback default_bundle)."""
    bundles_data: list[dict] = []
    policy_order: list[str] = []
    try:
        # try import policy_model directly
        import sys as _sys2
        from pathlib import Path as _P
        for _p in [str(_P(__file__).resolve().parents[2] / "packages" / "policy-model"), str(_P(__file__).resolve().parents[2] / "security" / "policy-engine")]:
            if _p not in _sys2.path:
                _sys2.path.insert(0, _p)
        from policy_model import POLICY_EVALUATION_ORDER  # type: ignore
        policy_order = [s.value for s in POLICY_EVALUATION_ORDER]
    except Exception:
        policy_order = ["explicit_deny","security_boundary_deny","personal_delegation","persistent_user_grant","group_grant","default_bundle","jit_approval","default_deny"]
    # try live security import
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
