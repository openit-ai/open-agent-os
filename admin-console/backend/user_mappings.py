"""Admin Console — Mattermost Users → Agent mapping API (§14 1인 1 Logical Agent).

In-memory store: MattermostMapping {id, mm_user_id, mm_username, employee_principal, agent_id, status, created_at, created_by}
Auto-derive agent_id = agent:assistant:{suffix} from employee principal.

Endpoints:
- GET  /v1/user-mappings          (list)           — L4 read (any authenticated)
- POST /v1/user-mappings          (register)       — L5 write
- DELETE /v1/user-mappings/{id}   (delete)         — L5 write
- POST /v1/user-mappings/sync     (dry-run preview) — L5 write (preview does not persist)

RBAC: L4 read, L5 write per §22.
Wire into app.py: from user_mappings import router as user_mappings_router; app.include_router(user_mappings_router)
DB persistence (AdminUserMappingORM) when DATABASE_URL/OAOS_DATABASE_URL set, fallback to dict.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import httpx

try:
    from .auth import AdminUser, get_current_admin, require_l5  # type: ignore
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

router = APIRouter(prefix="/v1/user-mappings", tags=["user-mappings"])

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class MattermostMapping(BaseModel):
    id: str
    mm_user_id: str
    mm_username: Optional[str] = None
    employee_principal: str
    agent_id: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    status: str = "active"
    created_at: datetime
    created_by: str

MAX_DISPLAY_NAME_LENGTH = 64
MAX_AVATAR_URL_LENGTH = 2048
_ALLOWED_AVATAR_SCHEMES = {"http", "https"}

def _validate_avatar_url(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if len(s) > MAX_AVATAR_URL_LENGTH:
        raise HTTPException(status_code=400, detail=f"avatar_url too long (max {MAX_AVATAR_URL_LENGTH})")
    # strict https/http only, bounded length, must have netloc
    try:
        from urllib.parse import urlparse
        parsed = urlparse(s)
        scheme = (parsed.scheme or "").lower()
        if scheme not in _ALLOWED_AVATAR_SCHEMES:
            raise HTTPException(status_code=400, detail="avatar_url must use http or https")
        if not parsed.netloc:
            raise HTTPException(status_code=400, detail="avatar_url must be absolute http(s) URL")
        # reject URLs with whitespace/control chars
        if any(c in s for c in (" ", "\n", "\r", "\t")):
            raise HTTPException(status_code=400, detail="avatar_url must not contain whitespace")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="invalid avatar_url")
    return s

class CreateMappingRequest(BaseModel):
    mm_user_id: Optional[str] = Field(default=None)
    mm_username: Optional[str] = None
    employee_principal: Optional[str] = None
    display_name: Optional[str] = Field(default=None, max_length=MAX_DISPLAY_NAME_LENGTH)
    avatar_url: Optional[str] = Field(default=None, max_length=MAX_AVATAR_URL_LENGTH)

class UpdateMappingRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=MAX_DISPLAY_NAME_LENGTH)
    avatar_url: Optional[str] = Field(default=None, max_length=MAX_AVATAR_URL_LENGTH)
    employee_principal: Optional[str] = None

class SyncRequest(BaseModel):
    users: Optional[list[dict]] = None  # each {mm_user_id, mm_username}

# ---------------------------------------------------------------------------
# In-memory store (fallback)
# ---------------------------------------------------------------------------
_mappings: dict[str, MattermostMapping] = {}

def clear_mappings() -> None:
    _mappings.clear()
    if _is_db_enabled():
        try:
            _db_clear_all()
        except Exception:
            pass

def list_mappings() -> list[MattermostMapping]:
    # try DB first
    if _is_db_enabled():
        items = _db_list_mappings()
        if items is not None:
            # sync dict mirror
            for m in items:
                _mappings[m.id] = m
            return sorted(items, key=lambda m: m.created_at, reverse=True)
    return sorted(_mappings.values(), key=lambda m: m.created_at, reverse=True)

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
    if "+aiosqlite" in u:
        u = u.replace("+aiosqlite", "")
        u = u.replace("sqlite+://", "sqlite://")
    if u.startswith("sqlite+"):
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


def _orm_to_mapping(row) -> MattermostMapping:
    # ORM has employee_id / employee_principal; prefer employee_principal then employee_id
    principal = getattr(row, "employee_principal", None) or getattr(row, "employee_id", None) or ""
    return MattermostMapping(
        id=row.id,
        mm_user_id=row.mm_user_id,
        mm_username=row.mm_username,
        employee_principal=principal,
        agent_id=row.agent_id or "",
        display_name=getattr(row, "display_name", None),
        avatar_url=getattr(row, "avatar_url", None),
        status=row.status or "active",
        created_at=row.created_at,
        created_by=row.created_by or "",
    )


def _db_clear_all() -> None:
    factory = _get_session_factory()
    if factory is None:
        return
    try:
        from security.models.orm import AdminUserMappingORM  # type: ignore

        with factory() as s:
            s.query(AdminUserMappingORM).delete()
            s.commit()
    except Exception:
        pass


def _db_list_mappings() -> list[MattermostMapping] | None:
    if not _is_db_enabled():
        return None
    factory = _get_session_factory()
    if factory is None:
        return None
    try:
        from security.models.orm import AdminUserMappingORM  # type: ignore

        with factory() as s:
            rows = s.query(AdminUserMappingORM).order_by(AdminUserMappingORM.created_at.desc()).all()
            return [_orm_to_mapping(r) for r in rows]
    except Exception:
        return None


def _db_get_mapping(mid: str) -> MattermostMapping | None:
    if not _is_db_enabled():
        return None
    factory = _get_session_factory()
    if factory is None:
        return None
    try:
        from security.models.orm import AdminUserMappingORM  # type: ignore

        with factory() as s:
            row = s.query(AdminUserMappingORM).filter(AdminUserMappingORM.id == mid).first()
            if row is None:
                return None
            return _orm_to_mapping(row)
    except Exception:
        return None


def _db_exists_mm_user_id(mm_user_id: str) -> bool | None:
    if not _is_db_enabled():
        return None
    factory = _get_session_factory()
    if factory is None:
        return None
    try:
        from security.models.orm import AdminUserMappingORM  # type: ignore

        with factory() as s:
            exists = s.query(AdminUserMappingORM).filter(AdminUserMappingORM.mm_user_id == mm_user_id).first() is not None
            return exists
    except Exception:
        return None


def _db_create_mapping(m: MattermostMapping) -> bool:
    if not _is_db_enabled():
        return False
    factory = _get_session_factory()
    if factory is None:
        return False
    try:
        from security.models.orm import AdminUserMappingORM  # type: ignore

        with factory() as s:
            orm = AdminUserMappingORM(
                id=m.id,
                mm_user_id=m.mm_user_id,
                mm_username=m.mm_username,
                employee_principal=m.employee_principal,
                employee_id=m.employee_principal,  # compat column
                agent_id=m.agent_id,
                display_name=m.display_name,
                avatar_url=m.avatar_url,
                status=m.status,
                created_at=m.created_at,
                created_by=m.created_by,
            )
            s.add(orm)
            s.commit()
            return True
    except Exception:
        try:
            with factory() as s2:
                s2.rollback()
        except Exception:
            pass
        return False


def _db_update_mapping(mid: str, display_name: str | None, avatar_url: str | None, employee_principal: str | None = None) -> MattermostMapping | None:
    if not _is_db_enabled():
        return None
    # validate avatar_url early even for DB path (strict https/http, bounded)
    if avatar_url is not None:
        avatar_url = _validate_avatar_url(avatar_url)
    factory = _get_session_factory()
    if factory is None:
        return None
    try:
        from security.models.orm import AdminUserMappingORM  # type: ignore
        with factory() as s:
            row = s.query(AdminUserMappingORM).filter(AdminUserMappingORM.id == mid).first()
            if row is None:
                return None
            if display_name is not None:
                row.display_name = display_name.strip() or None  # type: ignore
            if avatar_url is not None:
                row.avatar_url = avatar_url.strip() if avatar_url else None  # type: ignore
            if employee_principal is not None:
                row.employee_principal = employee_principal  # type: ignore
                row.employee_id = employee_principal  # type: ignore
                # derive new agent_id if principal changed
                suffix = employee_principal.split(":", 1)[1] if ":" in employee_principal else employee_principal
                row.agent_id = f"agent:assistant:{suffix}"  # type: ignore
            s.commit()
            return _orm_to_mapping(row)
    except Exception:
        return None

def _db_delete_mapping(mid: str) -> bool | None:
    if not _is_db_enabled():
        return None
    factory = _get_session_factory()
    if factory is None:
        return None
    try:
        from security.models.orm import AdminUserMappingORM  # type: ignore

        with factory() as s:
            row = s.query(AdminUserMappingORM).filter(AdminUserMappingORM.id == mid).first()
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Helpers — auto-derive logic (mirrors MattermostAdapter.map_mattermost_user)
# ---------------------------------------------------------------------------

def _derive_employee_principal(mm_user_id: str, mm_username: Optional[str]) -> str:
    """Mimic MattermostAdapter.map_mattermost_user logic."""
    # Try to delegate to actual adapter if available (for parity)
    try:
        import sys
        from pathlib import Path
        ROOT = Path(__file__).resolve().parents[2]
        adapter_path = ROOT / "adapters" / "mattermost"
        if str(ROOT / "adapters") not in sys.path:
            sys.path.insert(0, str(ROOT / "adapters"))
        # Try import via mattermost.adapter
        try:
            from mattermost.adapter import MattermostAdapter  # type: ignore
            ad = MattermostAdapter()
            return ad.map_mattermost_user(mm_user_id, mm_username)
        except Exception:
            pass
    except Exception:
        pass
    # Fallback pure logic (same as adapter)
    raw = mm_username or mm_user_id
    suffix = re.sub(r"[^a-z0-9_.-]", "", raw.lower()) or "unknown"
    return f"employee:{suffix}"

def _derive_agent_id(employee_principal: str) -> str:
    """agent:assistant:{suffix} from employee principal."""
    if ":" in employee_principal:
        suffix = employee_principal.split(":", 1)[1]
    else:
        suffix = employee_principal
    # sanitize suffix same way (lowercase already)
    suffix = suffix.strip()
    if not suffix:
        suffix = "unknown"
    return f"agent:assistant:{suffix}"

def _validate_principal(principal: str) -> None:
    if not principal.startswith("employee:"):
        raise HTTPException(status_code=400, detail="employee_principal must start with 'employee:'")

def _load_mm_config() -> tuple[str | None, str | None]:
    url = os.getenv("MATTERMOST_URL")
    token = os.getenv("MATTERMOST_TOKEN")
    if url and token:
        return url, token
    # Never scan Hermes global env files. OAOS uses explicit service configuration;
    # same-OS-account sessions are not isolated by Unix file permissions.
    return url, token

def _is_mm_id(s: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z0-9]{26}", s.strip()))

def _resolve_username_to_id(username: str) -> tuple[str, dict] | None:
    """Try to resolve username -> mm_user_id via Mattermost API. Returns (id, raw_json) or None."""
    username = username.strip()
    if not username:
        return None
    url, token = _load_mm_config()
    if not url or not token:
        return None
    try:
        r = httpx.get(f"{url.rstrip('/')}/api/v4/users/username/{username}", headers={"Authorization": f"Bearer {token}"}, timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            return data.get("id"), data
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/resolve", response_model=None)
def resolve_mm_user(username: str = Query(..., min_length=1), admin: AdminUser = Depends(get_current_admin)):
    """GET /v1/user-mappings/resolve?username=mykim — resolve Mattermost username to 26-char ID."""
    username = username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    url, token = _load_mm_config()
    if not url or not token:
        raise HTTPException(status_code=503, detail="Mattermost not configured on server")
    try:
        r = httpx.get(f"{url.rstrip('/')}/api/v4/users/username/{username}", headers={"Authorization": f"Bearer {token}"}, timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            return {"found": True, "mm_user_id": data.get("id"), "mm_username": data.get("username"), "email": data.get("email"), "display_name": f"{data.get('first_name','')} {data.get('last_name','')}".strip()}
        elif r.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Mattermost user '{username}' not found")
        else:
            raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:300])

@router.get("", response_model=None)
@router.get("/", response_model=None)
def list_user_mappings(admin: AdminUser = Depends(get_current_admin)):
    """GET /v1/user-mappings — list all mappings. L4 read allowed."""
    items = [m.model_dump(mode="json") for m in list_mappings()]
    return {"mappings": items, "count": len(items), "items": items}

@router.post("", status_code=201, response_model=None)
@router.post("/", status_code=201, response_model=None)
def create_user_mapping(req: CreateMappingRequest, admin: AdminUser = Depends(require_l5)):
    """POST /v1/user-mappings — register mapping. L5 only.

    Body: mm_username (required), mm_user_id? (auto-resolved if omitted), employee_principal?
    - If no principal, auto-derive via MattermostAdapter.map_mattermost_user logic.
    """
    raw_user_id = (req.mm_user_id or "").strip()
    mm_username = req.mm_username.strip() if req.mm_username else None
    if mm_username == "":
        mm_username = None
    # Username-only required: either field suffices, but mm_username preferred
    if not raw_user_id and not mm_username:
        raise HTTPException(status_code=400, detail="mm_username required (MM User name is required)")
    # Determine mm_user_id: if empty, resolve from username; if username-like, auto-resolve
    mm_user_id = raw_user_id
    if not mm_user_id and mm_username:
        resolved = _resolve_username_to_id(mm_username)
        if resolved and resolved[0]:
            mm_user_id = resolved[0]
            if not mm_username:
                mm_username = resolved[1].get("username")
        else:
            raise HTTPException(status_code=404, detail=f"Mattermost user '{mm_username}' not found — check username or enter MM User ID manually")
    elif mm_user_id and not _is_mm_id(mm_user_id):
        resolved = _resolve_username_to_id(mm_user_id)
        if resolved and resolved[0]:
            if not mm_username:
                mm_username = resolved[1].get("username") or mm_user_id
            mm_user_id = resolved[0]
        elif mm_username:
            resolved2 = _resolve_username_to_id(mm_username)
            if resolved2 and resolved2[0]:
                mm_user_id = resolved2[0]
        # if still not a 26-char ID and no resolve, keep as-is for fallback (derive will sanitize) but prefer to error if clearly not ID
        # allow fallback: if resolve failed but username exists, use username-derived placeholder? No — require resolve
        if not _is_mm_id(mm_user_id) and mm_username:
            # last attempt: if mm_user_id was meant to be username, error with guidance
            pass

    if req.employee_principal:
        principal = req.employee_principal.strip()
        _validate_principal(principal)
    else:
        principal = _derive_employee_principal(mm_user_id, mm_username)

    agent_id = _derive_agent_id(principal)
    display_name = (req.display_name or "").strip() or None
    # sanitize display_name length 64 already validated; empty -> None (fallback to username)
    if display_name and len(display_name) > MAX_DISPLAY_NAME_LENGTH:
        display_name = display_name[:MAX_DISPLAY_NAME_LENGTH]
    avatar_url = _validate_avatar_url(req.avatar_url)

    # optional: prevent duplicate mm_user_id — check DB if enabled, else dict
    if _is_db_enabled():
        dup = _db_exists_mm_user_id(mm_user_id)
        if dup is True:
            raise HTTPException(status_code=409, detail=f"mapping for mm_user_id {mm_user_id} already exists")
        if dup is None:
            # DB unreachable, fallback to dict check
            for existing in _mappings.values():
                if existing.mm_user_id == mm_user_id:
                    raise HTTPException(status_code=409, detail=f"mapping for mm_user_id {mm_user_id} already exists")
        else:
            # dup is False -> not exists, continue; also check dict mirror
            for existing in _mappings.values():
                if existing.mm_user_id == mm_user_id:
                    raise HTTPException(status_code=409, detail=f"mapping for mm_user_id {mm_user_id} already exists")
    else:
        for existing in _mappings.values():
            if existing.mm_user_id == mm_user_id:
                raise HTTPException(status_code=409, detail=f"mapping for mm_user_id {mm_user_id} already exists")

    mid = f"map_{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    mapping = MattermostMapping(
        id=mid,
        mm_user_id=mm_user_id,
        mm_username=mm_username,
        employee_principal=principal,
        agent_id=agent_id,
        display_name=display_name,
        avatar_url=avatar_url,
        status="active",
        created_at=now,
        created_by=admin.email,
    )
    # try DB persist first
    if _is_db_enabled():
        ok = _db_create_mapping(mapping)
        if ok:
            _mappings[mid] = mapping
            return mapping.model_dump(mode="json")
        # if DB enabled but create failed due to unique constraint, surface 409
        # check if already exists
        if _db_exists_mm_user_id(mm_user_id) is True:
            raise HTTPException(status_code=409, detail=f"mapping for mm_user_id {mm_user_id} already exists")
        # if DB error, fallback to dict
    _mappings[mid] = mapping
    return mapping.model_dump(mode="json")

@router.delete("/{mapping_id}", response_model=None)
def delete_user_mapping(mapping_id: str, admin: AdminUser = Depends(require_l5)):
    """DELETE /v1/user-mappings/{id} — L5 only."""
    if _is_db_enabled():
        res = _db_delete_mapping(mapping_id)
        if res is True:
            _mappings.pop(mapping_id, None)
            return {"status": "deleted", "id": mapping_id}
        if res is False:
            # not found in DB — check dict
            if mapping_id not in _mappings:
                raise HTTPException(status_code=404, detail="mapping not found")
            del _mappings[mapping_id]
            return {"status": "deleted", "id": mapping_id}
        # None -> DB error, fallback to dict
    if mapping_id not in _mappings:
        raise HTTPException(status_code=404, detail="mapping not found")
    del _mappings[mapping_id]
    return {"status": "deleted", "id": mapping_id}

@router.patch("/{mapping_id}", response_model=None)
def update_user_mapping(mapping_id: str, req: UpdateMappingRequest, admin: AdminUser = Depends(require_l5)):
    """PATCH /v1/user-mappings/{id} — update display_name / avatar_url (A안 개인별 호칭). L5 only."""
    # validate display_name if provided
    if req.display_name is not None:
        dn = req.display_name.strip()
        if dn == "":
            dn = None
        elif len(dn) > MAX_DISPLAY_NAME_LENGTH:
            raise HTTPException(status_code=400, detail="display_name too long (max 64)")
        req.display_name = dn
    # validate avatar_url if provided (https/http only, bounded 2048)
    if req.avatar_url is not None:
        req.avatar_url = _validate_avatar_url(req.avatar_url)
    # try DB first
    if _is_db_enabled():
        updated = _db_update_mapping(mapping_id, req.display_name, req.avatar_url, req.employee_principal)
        if updated is not None:
            _mappings[mapping_id] = updated
            return updated.model_dump(mode="json")
        # if DB enabled but not found via DB, check dict fallback
        existing = _db_get_mapping(mapping_id)
        if existing is None and mapping_id not in _mappings:
            raise HTTPException(status_code=404, detail="mapping not found")
    if mapping_id not in _mappings:
        raise HTTPException(status_code=404, detail="mapping not found")
    rec = _mappings[mapping_id]
    # apply in-memory
    data = rec.model_dump()
    if req.display_name is not None:
        data["display_name"] = req.display_name
    if req.avatar_url is not None:
        data["avatar_url"] = req.avatar_url.strip() or None
    if req.employee_principal is not None:
        _validate_principal(req.employee_principal.strip())
        data["employee_principal"] = req.employee_principal.strip()
        data["agent_id"] = _derive_agent_id(data["employee_principal"])
    updated_rec = MattermostMapping(**data)
    _mappings[mapping_id] = updated_rec
    return updated_rec.model_dump(mode="json")

@router.post("/sync", response_model=None)
def sync_preview(body: Optional[SyncRequest] = None, admin: AdminUser = Depends(require_l5)):
    """POST /v1/user-mappings/sync — dry-run list derived mappings for preview. L5 only.

    Accepts optional {users: [{mm_user_id, mm_username}]}. If not provided but body is empty dict,
    returns preview of currently stored mappings as derived view. If users list provided, derives
    mappings without persisting (dry-run).

    Returns {preview: [...], count, dry_run: true}
    """
    # body may be None if no json sent
    users_input: list[dict] = []
    if body is not None and body.users is not None:
        users_input = body.users

    preview: list[dict] = []

    if users_input:
        for u in users_input:
            mm_user_id = str(u.get("mm_user_id") or u.get("mmUserId") or "").strip()
            if not mm_user_id:
                continue
            mm_username = u.get("mm_username") or u.get("mmUsername")
            if mm_username is not None:
                mm_username = str(mm_username).strip() or None
            # allow explicit principal override per entry, else derive
            principal = u.get("employee_principal") or u.get("employeePrincipal")
            if principal:
                principal = str(principal).strip()
            else:
                principal = _derive_employee_principal(mm_user_id, mm_username)
            agent_id = _derive_agent_id(principal)
            preview.append({
                "mm_user_id": mm_user_id,
                "mm_username": mm_username,
                "employee_principal": principal,
                "agent_id": agent_id,
                "status": "active",
                "derived": True,
            })
    else:
        # No users provided — preview from stored mappings (derived view)
        # Use list_mappings() which is DB-aware
        for m in list_mappings():
            preview.append({
                "mm_user_id": m.mm_user_id,
                "mm_username": m.mm_username,
                "employee_principal": m.employee_principal,
                "agent_id": m.agent_id,
                "status": m.status,
                "id": m.id,
                "derived": False,
            })
        # If store empty, provide a sample structure hint (empty preview is valid)
    return {"preview": preview, "count": len(preview), "dry_run": True, "mappings": preview}
