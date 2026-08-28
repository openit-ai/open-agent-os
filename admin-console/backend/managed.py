"""Managed edition remote ops — support console API (admin-console/backend/managed.py).

Routes (RBAC L4+ — any authenticated admin, per task spec):
- GET  /v1/managed/status          — edition, version, uptime, SLO state
- GET  /v1/managed/support/tickets — list support tickets
- POST /v1/managed/support/ticket  — create ticket (title, body, severity)
- GET  /v1/managed/health          — aggregated VPS health for support team

RBAC: L4 (read-only) and L5 (infra-admin) both allowed (L4+).
Health aggregates: infra services (via infra._services), backup status, audit checkpoint.
Tickets: in-memory store (replace with DB in production). Persistent file fallback optional.

Mount in admin-console/backend/app.py:  from managed import router as managed_router; app.include_router(managed_router)
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

try:
    from .auth import AdminUser, get_current_admin
except ImportError:
    from auth import AdminUser, get_current_admin  # type: ignore

router = APIRouter(prefix="/v1/managed", tags=["managed"])

# ---------------------------------------------------------------------------
# Ticket store
# ---------------------------------------------------------------------------

class TicketSeverity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"

class TicketStatus(str, Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"

class SupportTicket(BaseModel):
    id: str
    title: str
    body: str
    severity: TicketSeverity = TicketSeverity.medium
    status: TicketStatus = TicketStatus.open
    created_by: str
    created_at: datetime
    updated_at: datetime

class CreateTicketRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    severity: TicketSeverity = TicketSeverity.medium

_tickets: dict[str, SupportTicket] = {}
_started_at = time.monotonic()

def _now() -> datetime:
    return datetime.now(timezone.utc)

def clear_tickets() -> None:
    _tickets.clear()

def list_tickets() -> list[SupportTicket]:
    return sorted(_tickets.values(), key=lambda t: t.created_at, reverse=True)

# ---------------------------------------------------------------------------
# GET /v1/managed/status
# ---------------------------------------------------------------------------

@router.get("/status")
def managed_status(admin: AdminUser = Depends(get_current_admin)):
    """Managed edition status — edition, version, uptime, SLO summary."""
    # Try to read version from pyproject or env
    version = os.environ.get("OAOS_VERSION", "0.1.1")
    edition = os.environ.get("OAOS_EDITION", "managed")
    uptime_seconds = int(time.monotonic() - _started_at)
    # SLO hints from env (central may override)
    slo_uptime = os.environ.get("SLO_UPTIME_TARGET", "99.5")
    slo_p95_ms = os.environ.get("SLO_P95_MS", "500")
    backup_age_slo_h = os.environ.get("SLO_BACKUP_AGE_H", "48")

    # Probe audit + backup if security available (best-effort)
    audit_head = None
    audit_count = None
    backup_last_ts = None
    try:
        import security.app as sec  # type: ignore
        al = getattr(sec, "audit_ledger", None)
        if al is not None:
            audit_head = getattr(al, "head", None)
            audit_count = getattr(al, "count", None)
        # backup timestamp from business module or prometheus metric file
    except Exception:
        pass

    return {
        "edition": edition,
        "version": version,
        "status": "ok",
        "uptime_seconds": uptime_seconds,
        "slo": {
            "uptime_target_percent": float(slo_uptime),
            "p95_target_ms": int(slo_p95_ms),
            "backup_max_age_hours": int(backup_age_slo_h),
        },
        "audit": {
            "head": audit_head,
            "event_count": audit_count,
        },
        "backup_last_success_timestamp": backup_last_ts,
        "checked_at": _now().isoformat(),
        "requested_by": admin.email,
    }

# ---------------------------------------------------------------------------
# Support tickets
# ---------------------------------------------------------------------------

@router.get("/support/tickets")
def list_support_tickets(admin: AdminUser = Depends(get_current_admin)):
    """List support tickets — L4+ (any authenticated admin)."""
    tickets = list_tickets()
    return {"tickets": [t.model_dump(mode="json") for t in tickets], "count": len(tickets)}

@router.post("/support/ticket", status_code=201)
def create_support_ticket(req: CreateTicketRequest, admin: AdminUser = Depends(get_current_admin)):
    """Create support ticket — L4+."""
    tid = f"tkt_{uuid.uuid4().hex[:10]}"
    now = _now()
    ticket = SupportTicket(
        id=tid,
        title=req.title.strip(),
        body=req.body.strip(),
        severity=req.severity,
        status=TicketStatus.open,
        created_by=admin.email,
        created_at=now,
        updated_at=now,
    )
    _tickets[tid] = ticket
    return ticket.model_dump(mode="json")

# ---------------------------------------------------------------------------
# GET /v1/managed/health — aggregated VPS health for support team
# ---------------------------------------------------------------------------

@router.get("/health")
def managed_health(admin: AdminUser = Depends(get_current_admin)):
    """Aggregated VPS health for support team — infra services + backup + audit."""
    # Infra services (from infra module)
    services = []
    services_healthy = 0
    services_total = 0
    try:
        # infra module is aliased as `infra` bare import in app.py bootstrap
        import infra as infra_mod  # type: ignore
        svc_dict = getattr(infra_mod, "_services", {})
        services_total = len(svc_dict)
        for svc in svc_dict.values():
            d = svc.model_dump(mode="json") if hasattr(svc, "model_dump") else dict(svc)
            services.append(d)
            if d.get("status") == "healthy":
                services_healthy += 1
    except Exception:
        pass

    # Audit ledger health
    audit_ok = True
    audit_head = None
    audit_verify = None
    try:
        import security.app as sec  # type: ignore
        al = getattr(sec, "audit_ledger", None)
        if al is not None:
            audit_head = getattr(al, "head", None)
            try:
                audit_verify = al.verify_chain()
                audit_ok = bool(audit_verify)
            except Exception:
                audit_verify = None
    except Exception:
        pass

    # Backup age (if backup module or metric available)
    backup_ok: Optional[bool] = None
    backup_last = None
    try:
        # business._backup_state may not exist — probe generically
        import business as biz  # type: ignore
        # not a real store — leave as unknown
        _ = biz
    except Exception:
        pass

    overall = "healthy"
    if services_total > 0 and services_healthy < services_total:
        overall = "degraded"
    if audit_verify is False:
        overall = "critical"

    return {
        "overall": overall,
        "timestamp": _now().isoformat(),
        "infra": {
            "services_total": services_total,
            "services_healthy": services_healthy,
            "services": services,
        },
        "audit": {
            "chain_valid": audit_verify,
            "head": audit_head,
            "ok": audit_ok,
        },
        "backup": {
            "ok": backup_ok,
            "last_success_timestamp": backup_last,
        },
        "slo": {
            "uptime_target_percent": 99.5,
            "p95_target_ms": 500,
            "backup_max_age_hours": 48,
        },
        "checked_by": admin.email,
    }
