"""Admin Console — InfraService (Section 22 + infra monitoring).

- InfraService model
- CRUD: POST /v1/infra/services, GET /v1/infra/services, PUT /{id}, DELETE /{id}
- Health probe: GET /v1/infra/health (httpx GET health_path, timeout 3s, status/latency, audit event)
- periodic check structure
- DB persistence (AdminInfraServiceORM) when DATABASE_URL/OAOS_DATABASE_URL set, fallback to dict
"""
from __future__ import annotations

import asyncio
import os
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
# In-memory store (fallback)
# ---------------------------------------------------------------------------
_services: dict[str, InfraService] = {}

# simple in-memory audit events for health probes
_audit_events: list[dict] = []


def clear_services() -> None:
    _services.clear()
    _audit_events.clear()
    # also clear DB when enabled — lazy, never raises, fallback to dict
    if _is_db_enabled():
        try:
            _db_clear_all()
        except Exception:
            pass


def _validate_name(name: str) -> None:
    if name not in ALLOWED_NAMES:
        raise HTTPException(status_code=400, detail=f"name must be one of {ALLOWED_NAMES}")


# ---------------------------------------------------------------------------
# DB persistence helpers — lazy, never import at top-level that breaks without DB
# ---------------------------------------------------------------------------
_db_engine = None
_db_session_factory = None  # type: ignore


def _db_url() -> str | None:
    url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if url and url.strip():
        return url.strip()
    return None


def _is_db_enabled() -> bool:
    try:
        u = _db_url()
        return bool(u)
    except Exception:
        return False


def _normalize_sync_url(url: str) -> str:
    u = url.strip()
    if u.startswith("postgresql+asyncpg://"):
        u = u.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    elif u.startswith("postgresql://"):
        u = u.replace("postgresql://", "postgresql+psycopg://", 1)
    # sqlite+aiosqlite -> sqlite
    if "+aiosqlite" in u:
        u = u.replace("+aiosqlite", "")
        # handle sqlite+aiosqlite:// -> sqlite://
        u = u.replace("sqlite+://", "sqlite://")
    if u.startswith("sqlite+"):
        # any remaining sqlite+ prefix
        u = u.replace("sqlite+", "sqlite", 1)
    return u


def _get_session_factory():
    global _db_engine, _db_session_factory
    if _db_session_factory is not None:
        return _db_session_factory
    url = _db_url()
    if not url:
        return None
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        sync_url = _normalize_sync_url(url)
        kwargs: dict = {"pool_pre_ping": True}
        if sync_url.startswith("sqlite"):
            kwargs = {}
            if ":memory:" in sync_url:
                kwargs["connect_args"] = {"check_same_thread": False}
        _db_engine = create_engine(sync_url, **kwargs)
        _db_session_factory = sessionmaker(bind=_db_engine, autoflush=False, autocommit=False)
        return _db_session_factory
    except Exception:
        return None


def _orm_to_service(row) -> InfraService:
    # row is AdminInfraServiceORM
    status_val = getattr(row, "status", "unknown") or "unknown"
    try:
        status = InfraStatus(status_val)
    except Exception:
        status = InfraStatus.unknown
    return InfraService(
        id=row.id,
        name=row.name,
        display_name=row.display_name,
        host=row.host,
        port=row.port,
        health_path=row.health_path or "/health",
        expected_status=row.expected_status or 200,
        last_check=row.last_check,
        status=status,
        latency_ms=row.latency_ms,
    )


def _db_clear_all() -> None:
    factory = _get_session_factory()
    if factory is None:
        return
    try:
        # lazy ORM import
        from security.models.orm import AdminInfraServiceORM  # type: ignore

        with factory() as s:
            s.query(AdminInfraServiceORM).delete()
            s.commit()
    except Exception:
        pass


def _db_list_services() -> list[InfraService] | None:
    if not _is_db_enabled():
        return None
    factory = _get_session_factory()
    if factory is None:
        return None
    try:
        from security.models.orm import AdminInfraServiceORM  # type: ignore

        with factory() as s:
            rows = s.query(AdminInfraServiceORM).all()
            return [_orm_to_service(r) for r in rows]
    except Exception:
        return None


def _db_get_service(sid: str) -> InfraService | None:
    if not _is_db_enabled():
        return None
    factory = _get_session_factory()
    if factory is None:
        return None
    try:
        from security.models.orm import AdminInfraServiceORM  # type: ignore

        with factory() as s:
            row = s.query(AdminInfraServiceORM).filter(AdminInfraServiceORM.id == sid).first()
            if row is None:
                return None
            return _orm_to_service(row)
    except Exception:
        return None


def _db_get_service_exists(sid: str) -> bool | None:
    """Return True/False if DB reachable, None if DB not enabled/unreachable (fallback to dict)."""
    if not _is_db_enabled():
        return None
    factory = _get_session_factory()
    if factory is None:
        return None
    try:
        from security.models.orm import AdminInfraServiceORM  # type: ignore

        with factory() as s:
            exists = s.query(AdminInfraServiceORM).filter(AdminInfraServiceORM.id == sid).first() is not None
            return exists
    except Exception:
        return None


def _db_create_service(svc: InfraService) -> bool:
    if not _is_db_enabled():
        return False
    factory = _get_session_factory()
    if factory is None:
        return False
    try:
        from security.models.orm import AdminInfraServiceORM  # type: ignore

        with factory() as s:
            orm = AdminInfraServiceORM(
                id=svc.id,
                name=svc.name,
                display_name=svc.display_name,
                host=svc.host,
                port=svc.port,
                health_path=svc.health_path,
                expected_status=svc.expected_status,
                status=svc.status.value if hasattr(svc.status, "value") else str(svc.status),
                latency_ms=svc.latency_ms,
                last_check=svc.last_check,
            )
            s.add(orm)
            s.commit()
            return True
    except Exception:
        try:
            # rollback on error
            with factory() as s2:
                s2.rollback()
        except Exception:
            pass
        return False


def _db_update_service(sid: str, data: dict) -> InfraService | None:
    if not _is_db_enabled():
        return None
    factory = _get_session_factory()
    if factory is None:
        return None
    try:
        from security.models.orm import AdminInfraServiceORM  # type: ignore

        with factory() as s:
            row = s.query(AdminInfraServiceORM).filter(AdminInfraServiceORM.id == sid).first()
            if row is None:
                return None
            for k, v in data.items():
                if k == "status" and hasattr(v, "value"):
                    v = v.value
                setattr(row, k, v)
            s.commit()
            s.refresh(row)
            return _orm_to_service(row)
    except Exception:
        return None


def _db_delete_service(sid: str) -> bool | None:
    """Return True if deleted, False if not found, None if DB not enabled/error (fallback)."""
    if not _is_db_enabled():
        return None
    factory = _get_session_factory()
    if factory is None:
        return None
    try:
        from security.models.orm import AdminInfraServiceORM  # type: ignore

        with factory() as s:
            row = s.query(AdminInfraServiceORM).filter(AdminInfraServiceORM.id == sid).first()
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True
    except Exception:
        return None


def _db_persist_probe(svc: InfraService) -> None:
    if not _is_db_enabled():
        return
    factory = _get_session_factory()
    if factory is None:
        return
    try:
        from security.models.orm import AdminInfraServiceORM  # type: ignore

        with factory() as s:
            row = s.query(AdminInfraServiceORM).filter(AdminInfraServiceORM.id == svc.id).first()
            if row is None:
                return
            row.status = svc.status.value if hasattr(svc.status, "value") else str(svc.status)
            row.latency_ms = svc.latency_ms
            row.last_check = svc.last_check
            s.commit()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Health probe logic
# ---------------------------------------------------------------------------
async def _probe_one(service: InfraService) -> InfraService:
    # Normalize host that may contain scheme/path (e.g. https://chat.openit.co.kr/api)
    raw_host = (service.host or "").strip()
    scheme = "http"
    host_clean = raw_host
    if raw_host.startswith("https://"):
        scheme = "https"
        host_clean = raw_host[8:]
    elif raw_host.startswith("http://"):
        scheme = "http"
        host_clean = raw_host[7:]
    # strip any trailing path from host
    if "/" in host_clean:
        host_clean = host_clean.split("/")[0]
    # port 443 implies https unless explicitly http
    if service.port == 443 and scheme == "http":
        scheme = "https"
    url = f"{scheme}://{host_clean}:{service.port}{service.health_path}"
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
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
    # Determine source: DB if enabled and reachable, else dict
    services = None
    if _is_db_enabled():
        try:
            db_items = _db_list_services()
            if db_items is not None:
                services = db_items
        except Exception:
            services = None
    if services is None:
        services = list(_services.values())
    results: list[InfraService] = []
    for svc in list(services):
        updated = await _probe_one(svc)
        # persist to dict fallback
        _services[updated.id] = updated
        # persist to DB if enabled
        try:
            _db_persist_probe(updated)
        except Exception:
            pass
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
    # try DB first
    if _is_db_enabled():
        ok = _db_create_service(svc)
        if ok:
            _services[sid] = svc
            return svc
        # if DB enabled but create failed (e.g. table missing), fallback to dict if error was not duplicate
        # Check if DB actually has it now
        existing = _db_get_service(sid)
        if existing is not None:
            _services[sid] = existing
            return existing
    _services[sid] = svc
    return svc


@router.get("/services", response_model=list[InfraService])
def list_services(admin: AdminUser = Depends(get_current_admin)):
    if _is_db_enabled():
        items = _db_list_services()
        if items is not None:
            # sync fallback dict for tests that inspect _services
            for it in items:
                _services[it.id] = it
            return items
    return list(_services.values())


@router.get("/services/{service_id}", response_model=InfraService)
def get_service(service_id: str, admin: AdminUser = Depends(get_current_admin)):
    if _is_db_enabled():
        svc = _db_get_service(service_id)
        if svc is not None:
            _services[service_id] = svc
            return svc
        # check if DB reachable but not found -> 404
        exists = _db_get_service_exists(service_id)
        if exists is False:
            raise HTTPException(status_code=404, detail="service not found")
        # if exists is None (DB error) -> fallback to dict
        # if exists is True but svc None shouldn't happen, fallback
    svc = _services.get(service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="service not found")
    return svc


@router.put("/services/{service_id}", response_model=InfraService)
def update_service(service_id: str, req: InfraServiceUpdate, admin: AdminUser = Depends(require_l5)):
    data = req.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        _validate_name(data["name"])
    # try DB path
    if _is_db_enabled():
        updated = _db_update_service(service_id, data)
        if updated is not None:
            _services[service_id] = updated
            return updated
        exists = _db_get_service_exists(service_id)
        if exists is False:
            raise HTTPException(status_code=404, detail="service not found")
        # if DB error (None) -> fallback to dict
    svc = _services.get(service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="service not found")
    for k, v in data.items():
        setattr(svc, k, v)
    _services[service_id] = svc
    return svc


@router.delete("/services/{service_id}")
def delete_service(service_id: str, admin: AdminUser = Depends(require_l5)):
    if _is_db_enabled():
        res = _db_delete_service(service_id)
        if res is True:
            _services.pop(service_id, None)
            return {"status": "deleted", "id": service_id}
        if res is False:
            # check dict fallback before 404? DB says not found
            if service_id in _services:
                del _services[service_id]
                return {"status": "deleted", "id": service_id}
            raise HTTPException(status_code=404, detail="service not found")
        # None -> DB error, fallback
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


def _to_alias_dict(svc: InfraService) -> dict:
    d = svc.model_dump(mode="json")
    d["service"] = d.get("name")
    return d


@router.get("", response_model=dict)
def list_services_alias(admin: AdminUser = Depends(get_current_admin)):
    if _is_db_enabled():
        items = _db_list_services()
        if items is not None:
            for it in items:
                _services[it.id] = it
            return {"items": [_to_alias_dict(s) for s in items]}
    items = list(_services.values())
    return {"items": [_to_alias_dict(s) for s in items]}


@router.post("", response_model=dict, status_code=201)
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
    if _is_db_enabled():
        ok = _db_create_service(svc)
        if ok:
            _services[sid] = svc
            return _to_alias_dict(svc)  # type: ignore[return-value]
        existing = _db_get_service(sid)
        if existing is not None:
            _services[sid] = existing
            return _to_alias_dict(existing)  # type: ignore[return-value]
    _services[sid] = svc
    return _to_alias_dict(svc)  # type: ignore[return-value]


@router.get("/{service_id}", response_model=dict)
def get_service_alias(service_id: str, admin: AdminUser = Depends(get_current_admin)):
    if _is_db_enabled():
        svc = _db_get_service(service_id)
        if svc is not None:
            _services[service_id] = svc
            return _to_alias_dict(svc)
        exists = _db_get_service_exists(service_id)
        if exists is False:
            raise HTTPException(status_code=404, detail="service not found")
    svc = _services.get(service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="service not found")
    return _to_alias_dict(svc)


@router.patch("/{service_id}", response_model=dict)
def patch_service_alias(service_id: str, req: InfraAliasUpdate, admin: AdminUser = Depends(require_l5)):
    data = req.model_dump(exclude_unset=True)
    # map service -> name
    if "service" in data and data["service"] is not None:
        data["name"] = data.pop("service")
    if "name" in data and data["name"] is not None:
        _validate_name(data["name"])
    if _is_db_enabled():
        updated = _db_update_service(service_id, data)
        if updated is not None:
            _services[service_id] = updated
            return _to_alias_dict(updated)  # type: ignore[return-value]
        exists = _db_get_service_exists(service_id)
        if exists is False:
            raise HTTPException(status_code=404, detail="service not found")
    svc = _services.get(service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="service not found")
    for k, v in data.items():
        setattr(svc, k, v)
    _services[service_id] = svc
    return _to_alias_dict(svc)  # type: ignore[return-value]


@router.delete("/{service_id}")
def delete_service_alias(service_id: str, admin: AdminUser = Depends(require_l5)):
    if _is_db_enabled():
        res = _db_delete_service(service_id)
        if res is True:
            _services.pop(service_id, None)
            return {"status": "deleted", "id": service_id}
        if res is False:
            if service_id in _services:
                del _services[service_id]
                return {"status": "deleted", "id": service_id}
            raise HTTPException(status_code=404, detail="service not found")
    if service_id not in _services:
        raise HTTPException(status_code=404, detail="service not found")
    del _services[service_id]
    return {"status": "deleted", "id": service_id}


@router.post("/{service_id}/probe", response_model=dict)
async def probe_one_alias(service_id: str, admin: AdminUser = Depends(get_current_admin)):
    # try DB first
    svc = None
    if _is_db_enabled():
        svc = _db_get_service(service_id)
        if svc is None:
            exists = _db_get_service_exists(service_id)
            if exists is False:
                raise HTTPException(status_code=404, detail="service not found")
    if svc is None:
        svc = _services.get(service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="service not found")
    updated = await _probe_one(svc)
    _services[service_id] = updated
    try:
        _db_persist_probe(updated)
    except Exception:
        pass
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
    return _to_alias_dict(updated)


@router.get("/audit/events")
def infra_audit_events(admin: AdminUser = Depends(get_current_admin)):
    return {"events": list(_audit_events)}
