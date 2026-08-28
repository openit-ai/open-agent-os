"""Admin Console — InfraService (Section 22 + infra monitoring).

- InfraService model
- CRUD: POST /v1/infra/services, GET /v1/infra/services, PUT /{id}, DELETE /{id}
- Health probe: GET /v1/infra/health (httpx GET health_path, timeout 3s, status/latency, audit event)
- periodic check structure
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

try:
    from .auth import AdminUser, get_current_admin, require_l5
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
ALLOWED_NAMES = ("mattermost", "outline", "hermes", "control-plane", "execution-gateway", "postgres", "redis", "security")


class InfraStatus(str, Enum):
    healthy = "healthy"
    unhealthy = "unhealthy"
    unknown = "unknown"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class InfraService(BaseModel):
    id: str
    name: str  # validated against ALLOWED_NAMES
    display_name: str
    host: str
    port: int = Field(ge=1, le=65535)
    health_path: str = "/health"
    expected_status: int = 200
    last_check: Optional[datetime] = None
    status: InfraStatus = InfraStatus.unknown
    latency_ms: Optional[float] = None


class InfraServiceCreate(BaseModel):
    name: str
    display_name: str
    host: str
    port: int = Field(ge=1, le=65535)
    health_path: str = "/health"
    expected_status: int = 200


class InfraServiceUpdate(BaseModel):
    display_name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    health_path: Optional[str] = None
    expected_status: Optional[int] = None
    name: Optional[str] = None


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------
_services: dict[str, InfraService] = {}

# simple in-memory audit events for health probes
_audit_events: list[dict] = []


def clear_services() -> None:
    _services.clear()
    _audit_events.clear()


def _validate_name(name: str) -> None:
    if name not in ALLOWED_NAMES:
        raise HTTPException(status_code=400, detail=f"name must be one of {ALLOWED_NAMES}")


# ---------------------------------------------------------------------------
# Health probe logic
# ---------------------------------------------------------------------------
async def _probe_one(service: InfraService) -> InfraService:
    url = f"http://{service.host}:{service.port}{service.health_path}"
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
            latency = (time.perf_counter() - start) * 1000
            service.latency_ms = round(latency, 2)
            service.last_check = datetime.now(timezone.utc)
            if resp.status_code == service.expected_status:
                service.status = InfraStatus.healthy
            else:
                service.status = InfraStatus.unhealthy
    except Exception:
        latency = (time.perf_counter() - start) * 1000
        service.latency_ms = round(latency, 2)
        service.last_check = datetime.now(timezone.utc)
        service.status = InfraStatus.unhealthy
    return service


async def probe_all_services() -> list[InfraService]:
    """Probe all registered services once, update status/latency, emit audit event."""
    results: list[InfraService] = []
    for svc in list(_services.values()):
        updated = await _probe_one(svc)
        _services[updated.id] = updated
        results.append(updated)
        # audit event
        _audit_events.append(
            {
                "event_id": f"evt_{uuid.uuid4().hex[:8]}",
                "type": "infra.health_probe",
                "service_id": svc.id,
                "service_name": svc.name,
                "status": updated.status.value,
                "latency_ms": updated.latency_ms,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
    return results


# periodic check structure
_periodic_task: Optional[asyncio.Task] = None


async def periodic_health_check(interval_seconds: int = 30) -> None:
    """Loop forever — probe all services every interval_seconds."""
    while True:
        try:
            await probe_all_services()
        except Exception:
            pass
        await asyncio.sleep(interval_seconds)


def start_periodic_check(interval_seconds: int = 30) -> asyncio.Task:
    """Start background periodic check (call on app startup)."""
    global _periodic_task
    if _periodic_task is not None and not _periodic_task.done():
        return _periodic_task
    loop = asyncio.get_event_loop()
    _periodic_task = loop.create_task(periodic_health_check(interval_seconds))
    return _periodic_task


def stop_periodic_check() -> None:
    global _periodic_task
    if _periodic_task is not None:
        _periodic_task.cancel()
        _periodic_task = None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/v1/infra", tags=["infra"])


@router.post("/services", response_model=InfraService, status_code=201)
def create_service(req: InfraServiceCreate, admin: AdminUser = Depends(require_l5)):
    _validate_name(req.name)
    sid = f"infra_{uuid.uuid4().hex[:8]}"
    svc = InfraService(
        id=sid,
        name=req.name,
        display_name=req.display_name,
        host=req.host,
        port=req.port,
        health_path=req.health_path,
        expected_status=req.expected_status,
    )
    _services[sid] = svc
    return svc


@router.get("/services", response_model=list[InfraService])
def list_services(admin: AdminUser = Depends(get_current_admin)):
    return list(_services.values())


@router.get("/services/{service_id}", response_model=InfraService)
def get_service(service_id: str, admin: AdminUser = Depends(get_current_admin)):
    svc = _services.get(service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="service not found")
    return svc


@router.put("/services/{service_id}", response_model=InfraService)
def update_service(service_id: str, req: InfraServiceUpdate, admin: AdminUser = Depends(require_l5)):
    svc = _services.get(service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="service not found")
    data = req.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        _validate_name(data["name"])
    for k, v in data.items():
        setattr(svc, k, v)
    _services[service_id] = svc
    return svc


@router.delete("/services/{service_id}")
def delete_service(service_id: str, admin: AdminUser = Depends(require_l5)):
    if service_id not in _services:
        raise HTTPException(status_code=404, detail="service not found")
    del _services[service_id]
    return {"status": "deleted", "id": service_id}


@router.get("/health")
async def health_probe(admin: AdminUser = Depends(get_current_admin)):
    """Probe all registered services (single shot)."""
    results = await probe_all_services()
    return {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "services": [s.model_dump(mode="json") for s in results],
        "audit_events_count": len(_audit_events),
    }


# ---------------------------------------------------------------------------
# Aliases for admin-console frontend (/v1/infra without /services)
# Frontend sends { service, host, port, health_path } and expects /v1/infra
# ---------------------------------------------------------------------------
class InfraAliasCreate(BaseModel):
    service: Optional[str] = None
    name: Optional[str] = None
    display_name: Optional[str] = None
    host: str
    port: int = Field(ge=1, le=65535)
    health_path: str = "/health"
    expected_status: int = 200

class InfraAliasUpdate(BaseModel):
    service: Optional[str] = None
    name: Optional[str] = None
    display_name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    health_path: Optional[str] = None
    expected_status: Optional[int] = None


def _resolve_name(data: dict) -> str:
    n = data.get("name") or data.get("service")
    if not n:
        raise HTTPException(status_code=400, detail="service/name is required")
    return n


@router.get("", response_model=dict)
def list_services_alias(admin: AdminUser = Depends(get_current_admin)):
    items = list(_services.values())
    return {"items": [s.model_dump(mode="json") for s in items]}


@router.post("", response_model=InfraService, status_code=201)
def create_service_alias(req: InfraAliasCreate, admin: AdminUser = Depends(require_l5)):
    data = req.model_dump(exclude_unset=True)
    name = _resolve_name(data)
    _validate_name(name)
    sid = f"infra_{uuid.uuid4().hex[:8]}"
    svc = InfraService(
        id=sid,
        name=name,
        display_name=data.get("display_name") or name,
        host=data["host"],
        port=data["port"],
        health_path=data.get("health_path", "/health"),
        expected_status=data.get("expected_status", 200),
    )
    _services[sid] = svc
    return svc


@router.get("/{service_id}", response_model=InfraService)
def get_service_alias(service_id: str, admin: AdminUser = Depends(get_current_admin)):
    svc = _services.get(service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="service not found")
    return svc


@router.patch("/{service_id}", response_model=InfraService)
def patch_service_alias(service_id: str, req: InfraAliasUpdate, admin: AdminUser = Depends(require_l5)):
    svc = _services.get(service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="service not found")
    data = req.model_dump(exclude_unset=True)
    # map service -> name
    if "service" in data and data["service"] is not None:
        data["name"] = data.pop("service")
    if "name" in data and data["name"] is not None:
        _validate_name(data["name"])
    for k, v in data.items():
        setattr(svc, k, v)
    _services[service_id] = svc
    return svc


@router.post("/{service_id}/probe", response_model=InfraService)
async def probe_one_alias(service_id: str, admin: AdminUser = Depends(get_current_admin)):
    svc = _services.get(service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="service not found")
    updated = await _probe_one(svc)
    _services[service_id] = updated
    _audit_events.append(
        {
            "event_id": f"evt_{uuid.uuid4().hex[:8]}",
            "type": "infra.health_probe",
            "service_id": svc.id,
            "service_name": svc.name,
            "status": updated.status.value,
            "latency_ms": updated.latency_ms,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    return updated


@router.get("/audit/events")
def infra_audit_events(admin: AdminUser = Depends(get_current_admin)):
    return {"events": list(_audit_events)}
