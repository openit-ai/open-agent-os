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
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import os
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
    status: str = "active"
    created_at: datetime
    created_by: str

class CreateMappingRequest(BaseModel):
    mm_user_id: Optional[str] = Field(default=None)
    mm_username: Optional[str] = None
    employee_principal: Optional[str] = None

class SyncRequest(BaseModel):
    users: Optional[list[dict]] = None  # each {mm_user_id, mm_username}

# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------
_mappings: dict[str, MattermostMapping] = {}

def clear_mappings() -> None:
    _mappings.clear()

def list_mappings() -> list[MattermostMapping]:
    return sorted(_mappings.values(), key=lambda m: m.created_at, reverse=True)

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
    # fallback: read from known env files
    for env_path in ["/home/openitsvc/.hermes/.env", "/root/.hermes/.env", os.path.expanduser("~/.hermes/.env")]:
        try:
            if os.path.exists(env_path):
                txt = open(env_path).read()
                for line in txt.splitlines():
                    if line.startswith("MATTERMOST_URL=") and not url:
                        url = line.split("=",1)[1].strip().strip('"')
                    if line.startswith("MATTERMOST_TOKEN=") and not token:
                        token = line.split("=",1)[1].strip().strip('"').strip()
        except Exception:
            pass
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

    # optional: prevent duplicate mm_user_id (return 409)
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
        status="active",
        created_at=now,
        created_by=admin.email,
    )
    _mappings[mid] = mapping
    return mapping.model_dump(mode="json")

@router.delete("/{mapping_id}", response_model=None)
def delete_user_mapping(mapping_id: str, admin: AdminUser = Depends(require_l5)):
    """DELETE /v1/user-mappings/{id} — L5 only."""
    if mapping_id not in _mappings:
        raise HTTPException(status_code=404, detail="mapping not found")
    del _mappings[mapping_id]
    return {"status": "deleted", "id": mapping_id}

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
        # Also provide derived example for known stored users? Return stored as preview
        for m in _mappings.values():
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
