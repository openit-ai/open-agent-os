"""Open Agent OS — Admin API (admin-console/backend/app.py).

- FastAPI(title="Open Agent OS Admin API")
- auth router + infra router
- Section 22 dashboard proxy: stats (users/policy/audit counts, approvals pending)
- CORS allow all
"""
from __future__ import annotations

import os
import sys

# Ensure sibling imports work when run as module or via pytest conftest SYS.PATH injection
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

try:
    from auth import router as auth_router, get_current_admin, AdminUser  # type: ignore
    from infra import router as infra_router  # type: ignore
except ImportError:
    from .auth import router as auth_router, get_current_admin, AdminUser  # type: ignore
    from .infra import router as infra_router  # type: ignore

app = FastAPI(title="Open Agent OS Admin API", version="1.1.0")

# ── CORS ─────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ──────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(infra_router)


# ── Health ───────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "admin-api"}


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
        # security/app.py is at repo_root/security/app.py — need to handle import
        # We attempt lazy import; if not on path, just keep defaults
        import importlib.util
        from pathlib import Path

        sec_app_path = Path(__file__).resolve().parents[2] / "security" / "app.py"
        if sec_app_path.exists():
            # use importlib to load without polluting
            # simpler: try sys.modules
            # attempt to import via existing security path
            # fallback: count from admin backend itself
            pass
    except Exception:
        pass

    # admin-local counts (always available)
    try:
        from auth import list_users

        users_count = len(list_users())
    except Exception:
        pass

    try:
        from infra import _services

        infra_count = len(_services)
    except Exception:
        infra_count = 0

    # try security stores if they are importable via conftest paths
    try:
        from delegation.delegation_service.service import DelegationService  # type: ignore

        # cannot get singleton count without store — just report 0 if not available
    except Exception:
        pass

    # Attempt to read security app globals if already imported
    try:
        import security.app as sec  # type: ignore

        # delegation_service has internal stores
        try:
            # DelegationService has _store or similar
            ds = getattr(sec, "delegation_service", None)
            if ds is not None:
                # try list_by_user fallback or internal dict
                if hasattr(ds, "_delegations"):
                    users_count = max(users_count, len(getattr(ds, "_delegations", {})))
                elif hasattr(ds, "_store"):
                    users_count = max(users_count, len(getattr(ds, "_store", {})))
        except Exception:
            pass
        try:
            al = getattr(sec, "audit_ledger", None)
            if al is not None:
                audit_count = al.count
        except Exception:
            pass
        try:
            pe = getattr(sec, "policy_engine", None)
            if pe is not None:
                # policy_engine.bundles
                bundles = getattr(pe, "bundles", [])
                policy_count = len(bundles)
        except Exception:
            pass
        try:
            aps = getattr(sec, "approval_store", None)
            if aps is not None:
                # approval store pending
                if hasattr(aps, "_store"):
                    store = getattr(aps, "_store", {})
                    pending_approvals = [
                        v for v in store.values() if getattr(v, "status", None) in ("pending", "PENDING") or str(getattr(v, "status", "")).lower() == "pending"
                    ]
                elif hasattr(aps, "list_pending"):
                    pending_approvals = aps.list_pending()  # type: ignore
        except Exception:
            pass
    except Exception:
        pass

    return {
        "users_count": users_count,
        "policy_count": policy_count,
        "audit_count": audit_count,
        "infra_services_count": infra_count,
        "pending_approvals_count": len(pending_approvals) if isinstance(pending_approvals, list) else 0,
    }


@app.get("/v1/dashboard/approvals")
def dashboard_approvals(admin: AdminUser = Depends(get_current_admin)):
    """Pending approvals list proxy."""
    # Try to fetch from security approval store
    pending = []
    try:
        import security.app as sec  # type: ignore

        aps = getattr(sec, "approval_store", None)
        if aps is not None:
            if hasattr(aps, "_store"):
                store = getattr(aps, "_store", {})
                for v in store.values():
                    st = str(getattr(v, "status", "")).lower()
                    if st == "pending":
                        # serialize
                        try:
                            pending.append(v.model_dump(mode="json"))  # type: ignore
                        except Exception:
                            pending.append({"id": getattr(v, "id", ""), "status": st})
            elif hasattr(aps, "list_pending"):
                raw = aps.list_pending()  # type: ignore
                for v in raw:
                    try:
                        pending.append(v.model_dump(mode="json"))  # type: ignore
                    except Exception:
                        pending.append(str(v))
    except Exception:
        pass
    return {"pending": pending, "count": len(pending)}
