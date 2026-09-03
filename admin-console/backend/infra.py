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
# Canonical 10 + legacy compat — single registry allows all known services without breaking existing CRUD
ALLOWED_NAMES = (
    "mattermost", "hermes", "outline", "postgres", "redis",
    "control-plane", "memory", "admin-api", "admin-console", "nginx",
    # legacy compat
    "execution-gateway", "security",
)
# Canonical 10 order for unified registry (display order)
CANONICAL_ORDER = (
    "mattermost", "hermes", "outline", "postgres", "redis",
    "control-plane", "memory", "admin-api", "admin-console", "nginx",
)
_CANONICAL_DISPLAY = {
    "mattermost": "Mattermost",
    "hermes": "Hermes",
    "outline": "Outline",
    "postgres": "PostgreSQL",
    "redis": "Redis",
    "control-plane": "Control Plane",
    "memory": "Memory",
    "admin-api": "Admin API",
    "admin-console": "Admin Console",
    "nginx": "nginx",
}
_CANONICAL_CATEGORY = {
    "mattermost": "collaboration",
    "hermes": "agent",
    "outline": "knowledge",
    "postgres": "datastore",
    "redis": "datastore",
    "control-plane": "platform",
    "memory": "platform",
    "admin-api": "platform",
    "admin-console": "platform",
    "nginx": "platform",
}
_CANONICAL_PROBE = {
    "postgres": "tcp",
    "redis": "tcp",
}


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
_TCP_NAMES = {"postgres", "redis"}

async def _probe_tcp(service: InfraService) -> InfraService:
    """TCP connect probe for non-HTTP services (postgres/redis)."""
    start = time.perf_counter()
    try:
        host_clean = (service.host or "").strip().removeprefix("https://").removeprefix("http://").split("/")[0]
        conn = await asyncio.wait_for(asyncio.open_connection(host_clean, service.port), timeout=3.0)
        # close cleanly
        try:
            writer = conn[1]
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        except Exception:
                pass
        latency = (time.perf_counter() - start) * 1000
        service.latency_ms = round(latency, 2)
        service.last_check = datetime.now(timezone.utc)
        service.status = InfraStatus.healthy
    except Exception:
        latency = (time.perf_counter() - start) * 1000
        service.latency_ms = round(latency, 2)
        service.last_check = datetime.now(timezone.utc)
        service.status = InfraStatus.unhealthy
    return service

async def _probe_one(service: InfraService) -> InfraService:
    # TCP probe for postgres/redis — no HTTP
    if service.name in _TCP_NAMES:
        return await _probe_tcp(service)
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


# ---------------------------------------------------------------------------
# Live System Inventory — read-only, no DB mutation
# Probes known live services independent of desired/managed InfraService records.
# Canonical non-secret entries for: Mattermost, Hermes, Outline, PostgreSQL, Redis
# + core platform (CP, memory, Admin API/Console, nginx).
# Probe types: http (GET health_path) vs tcp (raw TCP connect) — DB services
# use tcp without credentials (no password/DSN stored, host+port only).
# No automatic mutation of desired DB; live inventory is read-only.
# ---------------------------------------------------------------------------

def _live_http_entry(id_: str, name: str, display_name: str, host: str, port: int, health_path: str = "/health", expected_status: int = 200, extra: dict | None = None) -> dict:
    d: dict = {
        "id": id_,
        "name": name,
        "display_name": display_name,
        "host": host,
        "port": port,
        "health_path": health_path,
        "expected_status": expected_status,
        "probe_type": "http",
    }
    if extra:
        d.update(extra)
    return d


def _live_tcp_entry(id_: str, name: str, display_name: str, host: str, port: int, extra: dict | None = None) -> dict:
    d: dict = {
        "id": id_,
        "name": name,
        "display_name": display_name,
        "host": host,
        "port": port,
        "health_path": "/",
        "expected_status": 200,
        "probe_type": "tcp",
    }
    if extra:
        d.update(extra)
    return d


def _parse_host_port_from_url(url: str, default_host: str, default_port: int) -> tuple[str, int]:
    """Parse host/port from a URL without leaking credentials — returns (host, port)."""
    try:
        from urllib.parse import urlparse

        u = url.strip()
        if not u:
            return default_host, default_port
        # ensure scheme for urlparse
        if "://" not in u:
            u = "http://" + u
        p = urlparse(u)
        h = (p.hostname or default_host).strip()
        pt = p.port
        if pt is None:
            if p.scheme == "https":
                pt = 443
            else:
                pt = default_port
        return h, pt
    except Exception:
        return default_host, default_port


def _resolve_mattermost_live() -> dict:
    # non-secret: host/port only, no token
    raw = (os.environ.get("OAOS_CP_MATTERMOST_URL") or os.environ.get("MATTERMOST_URL") or "https://chat.openit.co.kr").strip()
    host, port = _parse_host_port_from_url(raw, "chat.openit.co.kr", 443)
    # Mattermost health via /api/v4/system/ping (returns {"status":"OK"}); port 443 implies https
    return _live_http_entry("live_mattermost", "mattermost", "Mattermost", host, port, health_path="/api/v4/system/ping", extra={"category": "collaboration", "url_hint": raw})


def _resolve_outline_live() -> dict:
    raw = (os.environ.get("OUTLINE_URL") or os.environ.get("OAOS_OUTLINE_URL") or os.environ.get("OUTLINE_API_URL") or "").strip()
    if raw:
        host, port = _parse_host_port_from_url(raw, "127.0.0.1", 3000)
        # Outline health: root or /_health — use / for broad compat without auth
        return _live_http_entry("live_outline", "outline", "Outline", host, port, health_path="/", extra={"category": "knowledge", "url_hint": raw})
    return _live_http_entry("live_outline", "outline", "Outline", "127.0.0.1", 3000, health_path="/", extra={"category": "knowledge"})


def _resolve_postgres_live() -> dict:
    # Derive host/port from DATABASE_URL without exposing password/DSN
    for key in ("OAOS_DATABASE_URL", "DATABASE_URL"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            try:
                from urllib.parse import urlparse

                # handle postgresql+asyncpg://, postgresql+psycopg://, postgresql://
                tmp = raw
                for pref in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql://"):
                    if tmp.startswith(pref):
                        tmp = "postgresql://" + tmp[len(pref) :]
                        break
                p = urlparse(tmp)
                h = (p.hostname or "127.0.0.1").strip()
                pt = p.port or 5432
                return _live_tcp_entry("live_postgres", "postgres", "PostgreSQL", h, pt, extra={"category": "datastore", "db": "oaos"})
            except Exception:
                pass
    # fallback to explicit POSTGRES_HOST/PORT or loopback
    host = (os.environ.get("POSTGRES_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.environ.get("POSTGRES_PORT") or "5432")
    except Exception:
        port = 5432
    return _live_tcp_entry("live_postgres", "postgres", "PostgreSQL", host, port, extra={"category": "datastore", "db": "oaos"})


def _resolve_redis_live() -> dict:
    raw = (os.environ.get("REDIS_URL") or "").strip()
    if raw:
        try:
            from urllib.parse import urlparse

            # redis://[:password@]host:port/db  or rediss://
            tmp = raw
            if tmp.startswith("redis://"):
                tmp = "redis://" + tmp[len("redis://") :]
            elif tmp.startswith("rediss://"):
                tmp = "redis://" + tmp[len("rediss://") :]
            p = urlparse(tmp if "://" in tmp else "redis://" + tmp)
            h = (p.hostname or "127.0.0.1").strip()
            pt = p.port or 6379
            return _live_tcp_entry("live_redis", "redis", "Redis", h, pt, extra={"category": "datastore"})
        except Exception:
            pass
    host = (os.environ.get("REDIS_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.environ.get("REDIS_PORT") or "6379")
    except Exception:
        port = 6379
    return _live_tcp_entry("live_redis", "redis", "Redis", host, port, extra={"category": "datastore"})


def _resolve_hermes_live() -> dict:
    raw = (os.environ.get("OAOS_CP_HERMES_BASE_URL") or os.environ.get("HERMES_BASE_URL") or "http://127.0.0.1:8642").strip()
    host, port = _parse_host_port_from_url(raw, "127.0.0.1", 8642)
    return _live_http_entry("live_hermes", "hermes", "Hermes Agent", host, port, health_path="/health", extra={"category": "agent"})


LIVE_INVENTORY: list[dict] = [
    _live_http_entry("live_cp", "control-plane", "Control Plane", "127.0.0.1", 8100, health_path="/health", extra={"category": "platform"}),
    _live_http_entry("live_memory", "memory", "Memory Service", "127.0.0.1", 8200, health_path="/health", extra={"category": "platform"}),
    _resolve_hermes_live(),
    _live_http_entry("live_admin_api", "admin-api", "Admin API", "127.0.0.1", 8010, health_path="/health", extra={"category": "platform"}),
    _live_http_entry("live_admin_console", "admin-console", "Admin Console", "127.0.0.1", 3012, health_path="/", extra={"category": "platform"}),
    _live_http_entry("live_nginx", "nginx", "Nginx (Admin Proxy 3000)", "192.168.6.61", 3000, health_path="/", extra={"category": "platform"}),
    _resolve_mattermost_live(),
    _resolve_outline_live(),
    _resolve_postgres_live(),
    _resolve_redis_live(),
]

async def _probe_live_one(entry: dict) -> dict:
    """Probe a single live inventory entry without touching DB/_services."""
    # Support probe_type override — tcp entries use TCP connect, http uses GET
    probe_type = (entry.get("probe_type") or ("tcp" if entry.get("name") in _TCP_NAMES else "http")).strip().lower()
    svc = InfraService(
        id=entry["id"],
        name=entry["name"],
        display_name=entry.get("display_name", entry["name"]),
        host=entry["host"],
        port=entry["port"],
        health_path=entry.get("health_path", "/health"),
        expected_status=entry.get("expected_status", 200),
    )
    # Inject probe_type-aware probing: temporarily set name mapping for _probe_one if needed
    # If entry declares tcp, force TCP path regardless of name
    if probe_type == "tcp":
        probed = await _probe_tcp(svc)
    else:
        probed = await _probe_one(svc)
    d = probed.model_dump(mode="json")
    # alias compat + live metadata
    d["service"] = d.get("name")
    d["live"] = True
    d["probe_type"] = probe_type
    d["category"] = entry.get("category")
    # Build canonical url: http for http, tcp:// for tcp (no credentials)
    # Correct HTTPS for port 443 or explicit https:// hint (Mattermost/Outline on 443)
    if probe_type == "tcp":
        d["url"] = f"tcp://{entry['host']}:{entry['port']}"
    else:
        hp = entry.get("health_path", "/health")
        # live Definition may carry url_hint with https; otherwise infer https for port 443
        raw_host = (entry.get("host") or "").strip()
        scheme = "https" if entry["port"] == 443 else "http"
        # If host string itself contains scheme (rare for live def), preserve it
        if raw_host.startswith("https://"):
            scheme = "https"
        elif raw_host.startswith("http://"):
            scheme = "http"
        hint = (entry.get("url_hint") or "").strip().lower()
        if hint.startswith("https://"):
            scheme = "https"
        # also if raw_host hint suggests https via url_hint
        # EnsureOutline/Mattermost on 443 always https even without hint
        d["url"] = f"{scheme}://{entry['host']}:{entry['port']}{hp}"
    # pass through non-secret extra for UI
    if entry.get("db"):
        d["db"] = entry["db"]
    return d


@router.get("/live")
async def live_inventory(admin: AdminUser = Depends(get_current_admin)):
    """Read-only live system inventory — probes fixed live services, no DB writes."""
    # probe concurrently with bounded timeout per entry ( _probe_one already 5s )
    results = await asyncio.gather(*[_probe_live_one(e) for e in LIVE_INVENTORY])
    return {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "count": len(results),
        "items": results,
        # compat alias for older frontend expects `live` key
        "live": results,
    }


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


# ---------------------------------------------------------------------------
# Unified canonical registry — single source of truth (DB desired + live probe in one row)
# GET /v1/infra/registry — merges DB registry with live probes, probes live concurrently
# POST /v1/infra/seed — idempotent seed of 10 canonical services (no secrets, host/port only)
# ---------------------------------------------------------------------------

def _canonical_defs_for_seed() -> list[dict]:
    """Safe canonical definitions for DB seed — host/port only, no secrets/DSN/password."""
    live_map = {e["name"]: e for e in LIVE_INVENTORY}
    out: list[dict] = []
    for name in CANONICAL_ORDER:
        e = live_map.get(name)
        if e is None:
            e = {"id": f"live_{name}", "name": name, "display_name": _CANONICAL_DISPLAY.get(name, name),
                 "host": "127.0.0.1", "port": 8000, "health_path": "/health", "probe_type": _CANONICAL_PROBE.get(name, "http"),
                 "category": _CANONICAL_CATEGORY.get(name, "platform")}
        out.append({
            "name": name,
            "display_name": e.get("display_name") or _CANONICAL_DISPLAY.get(name, name),
            "host": e["host"],
            "port": e["port"],
            "health_path": e.get("health_path", "/health"),
            "expected_status": e.get("expected_status", 200),
            "probe_type": e.get("probe_type") or _CANONICAL_PROBE.get(name, "http"),
            "category": e.get("category") or _CANONICAL_CATEGORY.get(name, "platform"),
        })
    return out


def _get_db_services_map() -> dict[str, "InfraService"]:
    """Return DB services keyed by name (first occurrence), fallback to dict."""
    items: list[InfraService] | None = None
    if _is_db_enabled():
        try:
            items = _db_list_services()
        except Exception:
            items = None
    if items is None:
        items = list(_services.values())
    by_name: dict[str, InfraService] = {}
    for it in items:
        if it.name not in by_name:
            by_name[it.name] = it
    return by_name


async def _build_unified_rows(probe: bool = True) -> list[dict]:
    """Build unified rows: canonical 10 + any extra DB names, with live probe merged."""
    db_by_name = _get_db_services_map()
    all_db_items: list[InfraService] = []
    if _is_db_enabled():
        try:
            tmp = _db_list_services()
            if tmp is not None:
                all_db_items = tmp
        except Exception:
            pass
    if not all_db_items:
        all_db_items = list(_services.values())

    live_by_name = {e["name"]: e for e in LIVE_INVENTORY}

    probed: dict[str, dict] = {}
    if probe:
        try:
            results = await asyncio.gather(*[_probe_live_one(e) for e in LIVE_INVENTORY])
            for r in results:
                probed[r.get("name") or r.get("service") or ""] = r
        except Exception:
            probed = {}
    else:
        for name, e in live_by_name.items():
            probed[name] = {
                "name": name,
                "service": name,
                "host": e["host"],
                "port": e["port"],
                "health_path": e.get("health_path", "/health"),
                "status": "unknown",
                "latency_ms": None,
                "last_check": None,
                "probe_type": e.get("probe_type", "http"),
                "category": e.get("category"),
                "url": (f"tcp://{e['host']}:{e['port']}" if e.get("probe_type") == "tcp" else f"http://{e['host']}:{e['port']}{e.get('health_path','/health')}"),
            }

    rows: list[dict] = []
    seen_names: set[str] = set()

    for name in CANONICAL_ORDER:
        db_svc = db_by_name.get(name)
        live_def = live_by_name.get(name)
        live_res = probed.get(name)
        if db_svc is not None:
            host = db_svc.host
            port = db_svc.port
            health_path = db_svc.health_path
            expected_status = db_svc.expected_status
            display_name = db_svc.display_name or _CANONICAL_DISPLAY.get(name, name)
            svc_id = db_svc.id
            source = "both" if live_def is not None else "db"
        elif live_def is not None:
            host = live_def["host"]
            port = live_def["port"]
            health_path = live_def.get("health_path", "/health")
            expected_status = live_def.get("expected_status", 200)
            display_name = live_def.get("display_name") or _CANONICAL_DISPLAY.get(name, name)
            svc_id = live_def["id"]
            source = "live"
        else:
            host = "127.0.0.1"
            port = 8000
            health_path = "/health"
            expected_status = 200
            display_name = _CANONICAL_DISPLAY.get(name, name)
            svc_id = f"live_{name}"
            source = "live"
        probe_type = (live_def.get("probe_type") if live_def else None) or _CANONICAL_PROBE.get(name, "http")
        category = (live_def.get("category") if live_def else None) or _CANONICAL_CATEGORY.get(name, "platform")
        # DB-backed rows: use DB host/port/health_path for probe and URL; preserve scheme/probe_type from live def or infer https for 443
        if db_svc is not None:
            if probe:
                try:
                    # Probe DB service directly so https://note.openit.co.kr:443/_health is used, not http://127.0.0.1:3000/
                    if probe_type == "tcp":
                        db_probed = await _probe_tcp(db_svc)
                    else:
                        db_probed = await _probe_one(db_svc)
                    status = db_probed.status.value if hasattr(db_probed.status, "value") else str(db_probed.status)
                    latency_ms = db_probed.latency_ms
                    last_check = db_probed.last_check.isoformat() if db_probed.last_check else None
                    # Build URL from DB host/port/health_path with correct scheme
                    if probe_type == "tcp":
                        url = f"tcp://{host}:{port}"
                    else:
                        hp = health_path or "/health"
                        # preserve scheme from live url_hint or infer https for 443
                        scheme = "https" if port == 443 else "http"
                        if live_def and (live_def.get("url_hint") or "").strip().lower().startswith("https://"):
                            scheme = "https"
                        # _probe_one already infers https for host with https:// or port 443, but URL must match probed scheme
                        # reuse probed URL scheme if available? ensure https for 443
                        url = f"{scheme}://{host}:{port}{hp}"
                    # persist probed state for health endpoint
                    _services[db_svc.id] = db_probed
                    try:
                        _db_persist_probe(db_probed)
                    except Exception:
                        pass
                except Exception:
                    # fallback to live_res if DB probe failed, but still build URL from DB
                    if live_res is not None:
                        status = live_res.get("status", "unknown")
                        latency_ms = live_res.get("latency_ms")
                        last_check = live_res.get("last_check")
                    else:
                        status = "unknown"
                        latency_ms = None
                        last_check = None
                    if probe_type == "tcp":
                        url = f"tcp://{host}:{port}"
                    else:
                        scheme = "https" if port == 443 else "http"
                        if live_def and (live_def.get("url_hint") or "").strip().lower().startswith("https://"):
                            scheme = "https"
                        url = f"{scheme}://{host}:{port}{health_path}"
            else:
                status = "unknown"
                latency_ms = None
                last_check = None
                if probe_type == "tcp":
                    url = f"tcp://{host}:{port}"
                else:
                    scheme = "https" if port == 443 else "http"
                    if live_def and (live_def.get("url_hint") or "").strip().lower().startswith("https://"):
                        scheme = "https"
                    url = f"{scheme}://{host}:{port}{health_path}"
        else:
            if live_res is not None:
                status = live_res.get("status", "unknown")
                latency_ms = live_res.get("latency_ms")
                last_check = live_res.get("last_check")
                url = live_res.get("url")
            else:
                status = "unknown"
                latency_ms = None
                last_check = None
                probe_t = live_def.get("probe_type") if live_def else _CANONICAL_PROBE.get(name, "http")
                url = f"tcp://{host}:{port}" if probe_t == "tcp" else f"http://{host}:{port}{health_path}"
                if live_def and live_def.get("url_hint", "").lower().startswith("https://"):
                    url = url.replace("http://", "https://", 1)
                # infer https for live 443
                if port == 443 and url.startswith("http://"):
                    url = url.replace("http://", "https://", 1)
        rows.append({
            "id": svc_id,
            "name": name,
            "service": name,
            "display_name": display_name,
            "host": host,
            "port": port,
            "health_path": health_path,
            "expected_status": expected_status,
            "status": status,
            "latency_ms": latency_ms,
            "last_check": last_check,
            "probe_type": probe_type,
            "category": category,
            "source": source,
            "url": url,
            "db_exists": db_svc is not None,
            "live_exists": live_def is not None,
        })
        seen_names.add(name)

    extras: list[InfraService] = [s for s in all_db_items if s.name not in seen_names]
    extra_by_name: dict[str, InfraService] = {}
    for s in extras:
        if s.name not in extra_by_name:
            extra_by_name[s.name] = s
    for name, db_svc in extra_by_name.items():
        live_res = probed.get(name)
        live_def = live_by_name.get(name)
        if live_res is not None:
            status = live_res.get("status", "unknown")
            latency_ms = live_res.get("latency_ms")
            last_check = live_res.get("last_check")
            url = live_res.get("url")
            probe_type = live_res.get("probe_type") or (live_def.get("probe_type") if live_def else "http")
            category = live_res.get("category") or (live_def.get("category") if live_def else None)
            source = "both"
        else:
            try:
                svc_probe = await _probe_one(db_svc)
                status = svc_probe.status.value if hasattr(svc_probe.status, "value") else str(svc_probe.status)
                latency_ms = svc_probe.latency_ms
                last_check = svc_probe.last_check.isoformat() if svc_probe.last_check else None
                _services[db_svc.id] = svc_probe
                try:
                    _db_persist_probe(svc_probe)
                except Exception:
                    pass
                probe_type = "tcp" if db_svc.name in _TCP_NAMES else "http"
                category = None
                url = f"tcp://{db_svc.host}:{db_svc.port}" if probe_type == "tcp" else f"http://{db_svc.host}:{db_svc.port}{db_svc.health_path}"
                source = "db"
            except Exception:
                status = "unknown"
                latency_ms = None
                last_check = None
                probe_type = "tcp" if db_svc.name in _TCP_NAMES else "http"
                category = None
                url = f"tcp://{db_svc.host}:{db_svc.port}" if probe_type == "tcp" else f"http://{db_svc.host}:{db_svc.port}{db_svc.health_path}"
                source = "db"
        rows.append({
            "id": db_svc.id,
            "name": db_svc.name,
            "service": db_svc.name,
            "display_name": db_svc.display_name,
            "host": db_svc.host,
            "port": db_svc.port,
            "health_path": db_svc.health_path,
            "expected_status": db_svc.expected_status,
            "status": status,
            "latency_ms": latency_ms,
            "last_check": last_check,
            "probe_type": probe_type,
            "category": category,
            "source": source,
            "url": url,
            "db_exists": True,
            "live_exists": live_def is not None,
        })

    return rows


@router.get("/registry")
async def unified_registry(admin: AdminUser = Depends(get_current_admin)):
    """Unified canonical registry — DB desired + live probe merged per row.

    Single Source of Truth: 등록·수정은 DB 기준, 모니터링(status/latency/last_check)은 live probe 기준.
    Columns per row: id, name/service, display_name, host, port, health_path, expected_status,
    status, latency_ms, last_check, probe_type, category, source (db/live/both), url.
    Existing CRUD (/v1/infra, /v1/infra/services) remains compatible.
    """
    rows = await _build_unified_rows(probe=True)
    return {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "items": rows,
        "registry": rows,
    }


@router.get("/unified")
async def unified_alias(admin: AdminUser = Depends(get_current_admin)):
    """Alias for /registry — compat."""
    rows = await _build_unified_rows(probe=True)
    return {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "items": rows,
        "registry": rows,
    }


@router.post("/seed")
def seed_canonical_registry(admin: AdminUser = Depends(require_l5)):
    """Idempotent seed of 10 canonical services into DB — host/port/health_path only, no secrets.

    Backup: before first insert, dumps existing admin_infra_services to timestamped JSON backup file.
    Idempotent: skips names already present. Returns created/skipped counts.
    """
    import json
    import pathlib

    defs = _canonical_defs_for_seed()
    backup_path: str | None = None
    try:
        existing_items: list[InfraService] | None = None
        if _is_db_enabled():
            existing_items = _db_list_services()
        if existing_items is None:
            existing_items = list(_services.values())
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_dir = pathlib.Path(__file__).resolve().parents[2] / f".backup_infra_seed_{ts}"
        if not backup_dir.parent.exists():
            backup_dir = pathlib.Path(".") / f".backup_infra_seed_{ts}"
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_file = backup_dir / "admin_infra_services_backup.json"
            dump = [s.model_dump(mode="json") if hasattr(s, "model_dump") else dict(s) for s in (existing_items or [])]
            backup_file.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
            backup_path = str(backup_file)
        except Exception:
            try:
                flat = pathlib.Path(f".backup_infra_seed_{ts}.json")
                flat.write_text(json.dumps([s.model_dump(mode="json") if hasattr(s, "model_dump") else dict(s) for s in (existing_items or [])], ensure_ascii=False, indent=2), encoding="utf-8")
                backup_path = str(flat)
            except Exception:
                backup_path = None
    except Exception:
        backup_path = None

    existing_names: set[str] = set()
    try:
        if _is_db_enabled():
            items = _db_list_services()
            if items is not None:
                existing_names = {s.name for s in items}
            else:
                existing_names = {s.name for s in _services.values()}
        else:
            existing_names = {s.name for s in _services.values()}
    except Exception:
        existing_names = {s.name for s in _services.values()}

    created: list[dict] = []
    skipped: list[str] = []
    for d in defs:
        name = d["name"]
        if name in existing_names:
            skipped.append(name)
            continue
        sid = f"infra_{name.replace('-', '_')}"
        check_id = sid
        suffix = 0
        while True:
            exists = False
            if _is_db_enabled():
                try:
                    exists = _db_get_service(check_id) is not None
                except Exception:
                    exists = check_id in _services
            else:
                exists = check_id in _services
            if not exists and check_id not in {s.id for s in _services.values()}:
                break
            suffix += 1
            check_id = f"{sid}_{suffix:02d}"
            if suffix > 20:
                check_id = f"infra_{uuid.uuid4().hex[:6]}"
                break
        svc = InfraService(
            id=check_id,
            name=name,
            display_name=d["display_name"],
            host=d["host"],
            port=d["port"],
            health_path=d["health_path"],
            expected_status=d["expected_status"],
        )
        ok = False
        if _is_db_enabled():
            ok = _db_create_service(svc)
        if ok:
            _services[check_id] = svc
            created.append(_to_alias_dict(svc))
        else:
            if not _is_db_enabled():
                _services[check_id] = svc
                created.append(_to_alias_dict(svc))
            else:
                try:
                    existing = _db_get_service(check_id)
                    if existing is not None:
                        skipped.append(name)
                    else:
                        _services[check_id] = svc
                        created.append(_to_alias_dict(svc))
                except Exception:
                    _services[check_id] = svc
                    created.append(_to_alias_dict(svc))
        existing_names.add(name)

    return {
        "status": "ok",
        "created_count": len(created),
        "skipped_count": len(skipped),
        "created": created,
        "skipped": skipped,
        "backup_path": backup_path,
        "note": "host/port/health_path only — no API key/password/DSN stored or displayed",
    }


@router.post("/upsert", response_model=dict, status_code=200)
def upsert_service_alias(req: InfraAliasCreate, admin: AdminUser = Depends(require_l5)):
    """Dedicated upsert for live-only edit — idempotent: create or update by canonical name.

    Non-secret fields only (host/port/health_path/expected_status). No password/DSN stored.
    Keeps probe_type/category metadata via LIVE_INVENTORY + CANONICAL maps, preserves tenant isolation.
    Used by frontend when Update is pressed on a live-only row (live_outline etc.).
    """
    data = req.model_dump(exclude_unset=True)
    name = _resolve_name(data)
    _validate_name(name)
    # If canonical DB row for this name exists, update it
    by_name = _get_db_services_map()
    existing = by_name.get(name)
    if existing is None and not _is_db_enabled():
        for _s in list(_services.values()):
            if _s.name == name:
                existing = _s
                break
    payload = {
        "host": data["host"],
        "port": data["port"],
        "health_path": data.get("health_path", "/health"),
        "expected_status": data.get("expected_status", 200),
        "display_name": data.get("display_name") or name,
        "name": name,
    }
    if existing is not None:
        if _is_db_enabled():
            updated = _db_update_service(existing.id, payload)
            if updated is not None:
                _services[existing.id] = updated
                return _to_alias_dict(updated)  # type: ignore[return-value]
        svc = _services.get(existing.id)
        if svc is not None:
            for k, v in payload.items():
                setattr(svc, k, v)
            _services[existing.id] = svc
            return _to_alias_dict(svc)  # type: ignore[return-value]
        # DB said exists but dict missing — try fetch
        if _is_db_enabled():
            fetched = _db_get_service(existing.id)
            if fetched is not None:
                _services[existing.id] = fetched
                return _to_alias_dict(fetched)  # type: ignore[return-value]
        raise HTTPException(status_code=500, detail="upsert failed: existing row not reachable")
    # create new
    base_id = f"infra_{name.replace('-', '_')}"
    new_id = base_id
    suffix_i = 0
    while True:
        exists = False
        if _is_db_enabled():
            try:
                exists = _db_get_service(new_id) is not None
            except Exception:
                exists = new_id in _services
        else:
            exists = new_id in _services
        if not exists and new_id not in {s.id for s in _services.values()}:
            break
        suffix_i += 1
        new_id = f"{base_id}_{suffix_i:02d}"
        if suffix_i > 20:
            new_id = f"infra_{uuid.uuid4().hex[:6]}"
            break
    svc_new = InfraService(
        id=new_id,
        name=name,
        display_name=payload["display_name"] or name,
        host=payload["host"],
        port=payload["port"],
        health_path=payload["health_path"],
        expected_status=payload["expected_status"],
    )
    if _is_db_enabled():
        ok = _db_create_service(svc_new)
        if ok:
            _services[new_id] = svc_new
            return _to_alias_dict(svc_new)  # type: ignore[return-value]
        existing2 = _db_get_service(new_id)
        if existing2 is not None:
            _services[new_id] = existing2
            return _to_alias_dict(existing2)  # type: ignore[return-value]
        _services[new_id] = svc_new
        return _to_alias_dict(svc_new)  # type: ignore[return-value]
    _services[new_id] = svc_new
    return _to_alias_dict(svc_new)  # type: ignore[return-value]


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
    # Live-only upsert: editing a live_ synthetic id must register it into DB (user clicked Update)
    # Never silently write before click; this path only runs on explicit PATCH from frontend.
    # Preserves L5 (require_l5), no secrets stored, keeps probe_type/category/url via live metadata.
    is_live_id = service_id.startswith("live_")
    if is_live_id:
        # Resolve live definition and target name
        live_def = next((e for e in LIVE_INVENTORY if e["id"] == service_id), None)
        # derive name from payload or live_def or id suffix
        derived_name = None
        if data.get("name"):
            derived_name = data.get("name")
        elif live_def is not None:
            derived_name = live_def.get("name")
        else:
            # fallback: live_outline -> outline, live_admin_api -> admin-api etc.
            suffix = service_id[5:]
            # try to reverse underscore mapping for infra names
            # live_ -> direct name lookup: outline, redis, etc. canonical uses - for admin-api
            candidate = suffix.replace("_", "-") if suffix in ("admin_api", "admin_console", "control_plane") else suffix
            # special: admin_api/control_plane mapping
            _live_suffix_map = {"admin_api": "admin-api", "admin_console": "admin-console", "control_plane": "control-plane"}
            candidate = _live_suffix_map.get(suffix, suffix)
            derived_name = candidate
        if not derived_name:
            raise HTTPException(status_code=400, detail="service/name is required for live registration")
        _validate_name(derived_name)
        # If DB already has a service with this name, update that existing row (idempotent upsert)
        existing_by_name = _get_db_services_map().get(derived_name)  # type: ignore
        # Also check in-memory fallback when DB not enabled
        if existing_by_name is None and not _is_db_enabled():
            for _s in list(_services.values()):
                if _s.name == derived_name:
                    existing_by_name = _s
                    break
        if existing_by_name is not None:
            # update existing DB row with supplied fields (host/port/health_path/expected_status/display_name)
            upd_data = {k: v for k, v in data.items() if v is not None}
            # ensure name is set for consistency
            if "name" not in upd_data:
                upd_data["name"] = derived_name
            else:
                upd_data["name"] = derived_name
            # try DB update path
            if _is_db_enabled():
                updated = _db_update_service(existing_by_name.id, upd_data)
                if updated is not None:
                    _services[existing_by_name.id] = updated
                    return _to_alias_dict(updated)  # type: ignore[return-value]
                # fallback to dict if DB failed but dict has it
            svc = _services.get(existing_by_name.id)
            if svc is not None:
                for k, v in upd_data.items():
                    setattr(svc, k, v)
                _services[existing_by_name.id] = svc
                return _to_alias_dict(svc)  # type: ignore[return-value]
            # if not in dict but DB says exists, return updated from DB retry
            raise HTTPException(status_code=404, detail="service not found")
        # No existing DB row for this name — create new DB entry from live_def + payload
        # host/port/health_path from payload override live_def defaults; no secrets ever stored
        host = data.get("host") or (live_def.get("host") if live_def else None) or "127.0.0.1"
        port = data.get("port")
        if port is None and live_def is not None:
            port = live_def.get("port")
        if port is None:
            raise HTTPException(status_code=400, detail="host and port are required for live registration")
        try:
            port = int(port)  # type: ignore
        except Exception:
            raise HTTPException(status_code=400, detail="port must be integer 1-65535")
        if not (1 <= port <= 65535):
            raise HTTPException(status_code=400, detail="port must be integer 1-65535")
        health_path = data.get("health_path") or (live_def.get("health_path") if live_def else None) or "/health"
        expected_status = data.get("expected_status") or (live_def.get("expected_status") if live_def else None) or 200  # type: ignore
        display_name = data.get("display_name") or (live_def.get("display_name") if live_def else None) or derived_name
        # allocate stable id like seed: infra_{name} with collision suffix
        base_id = f"infra_{derived_name.replace('-', '_')}"
        new_id = base_id
        suffix_i = 0
        while True:
            exists = False
            if _is_db_enabled():
                try:
                    exists = _db_get_service(new_id) is not None
                except Exception:
                    exists = new_id in _services
            else:
                exists = new_id in _services
            if not exists and new_id not in {s.id for s in _services.values()}:
                break
            suffix_i += 1
            new_id = f"{base_id}_{suffix_i:02d}"
            if suffix_i > 20:
                new_id = f"infra_{uuid.uuid4().hex[:6]}"
                break
        svc_new = InfraService(
            id=new_id,
            name=derived_name,
            display_name=display_name or derived_name,
            host=host,
            port=port,
            health_path=health_path,
            expected_status=expected_status,
        )
        if _is_db_enabled():
            ok = _db_create_service(svc_new)
            if ok:
                _services[new_id] = svc_new
                return _to_alias_dict(svc_new)  # type: ignore[return-value]
            # if DB create failed but row now exists (race), try to fetch and return
            existing = _db_get_service(new_id)
            if existing is not None:
                _services[new_id] = existing
                return _to_alias_dict(existing)  # type: ignore[return-value]
            # fallback to dict when DB enabled but write failed (table missing etc.)
            _services[new_id] = svc_new
            return _to_alias_dict(svc_new)  # type: ignore[return-value]
        _services[new_id] = svc_new
        return _to_alias_dict(svc_new)  # type: ignore[return-value]
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
