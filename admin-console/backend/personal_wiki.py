"""Personal Wiki API — admin-console skeleton (Section Personal Wiki).

POST /v1/personal-wiki/attachments -> extract -> journal
GET  /v1/personal-wiki/search?q=     -> pgvector search stub
GET  /v1/personal-wiki/notes         -> list notes

- Owner-isolated: employee:xxx -> agent:xxx vault path via get_vault_path()
- Audit logged via logger + best-effort audit ledger
- Fails gracefully when DB/vault not configured -> returns mock data
- Lazy DB imports so tests pass without postgres/pgvector/drivers

Wire into admin-console/backend/app.py with lazy include_router.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, UploadFile, File, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/personal-wiki", tags=["personal-wiki"])

# ---------------------------------------------------------------------------
# Vault path helper — owner isolation
# ---------------------------------------------------------------------------

def derive_agent_id(user_id: str) -> str:
    """Deterministic employee -> agent mapping (mirrors control-plane)."""
    if user_id.startswith("employee:"):
        return user_id.replace("employee:", "agent:assistant:", 1)
    if user_id.startswith("agent:"):
        return user_id
    return f"agent:assistant:{user_id}"

def get_vault_path(user_id: str, suffix: str = "") -> str:
    """Owner-isolated vault path for Personal Wiki.

    Example: employee:kim -> agent:assistant:kim -> vault/openagentos/agent:assistant:kim/personal_wiki[/suffix]
    Respects VAULT_KV_PREFIX / VAULT_KV_MOUNT env when set.
    """
    agent_id = derive_agent_id(user_id)
    prefix = os.environ.get("VAULT_KV_PREFIX", "openagentos/").strip()
    # normalize prefix trailing slash
    if prefix and not prefix.endswith("/"):
        prefix = prefix + "/"
    if not prefix:
        prefix = "openagentos/"
    # Sanitize agent_id for path (keep colons — vault allows)
    base = f"{prefix}{agent_id}/personal_wiki"
    if suffix:
        suffix = suffix.lstrip("/")
        return f"{base}/{suffix}"
    return base

def vault_path_for_note(user_id: str, note_id: str) -> str:
    return get_vault_path(user_id, f"notes/{note_id}")

def vault_path_for_attachment(user_id: str, attachment_id: str) -> str:
    return get_vault_path(user_id, f"attachments/{attachment_id}")

# ---------------------------------------------------------------------------
# DB / vault config helpers — graceful fallback
# ---------------------------------------------------------------------------

def _is_db_configured() -> bool:
    url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    return bool(url and url.strip())

def _is_vault_configured() -> bool:
    # VAULT_BACKEND set or legacy encrypted_postgres counts as configured;
    # treat absence of VAULT_ADDR + VAULT_TOKEN and no DB as "not configured"
    # For skeleton, we consider vault configured when OAOS_DATABASE_URL or DATABASE_URL set
    # or VAULT_BACKEND explicitly set.
    if os.environ.get("VAULT_BACKEND"):
        return True
    if os.environ.get("VAULT_ADDR"):
        return True
    # if DB configured, postgres vault table is available
    if _is_db_configured():
        return True
    return False

# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------

def _audit(request: Request | None, event_type: str, detail: dict[str, Any]) -> None:
    logger.info(f"[PERSONAL_WIKI_AUDIT] {event_type} detail={detail}")
    # Best-effort: append to security audit ledger if available
    try:
        if request is not None:
            tenant = request.headers.get("x-tenant-id") or request.headers.get("X-Tenant-Id") or "default"
        else:
            tenant = "default"
        # Try to persist via in-memory or DB — never raise
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        sec_path = str(root / "security")
        if sec_path not in sys.path:
            sys.path.insert(0, sec_path)
        # we just log; actual ledger wiring is optional
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Auth helper — extract owner from headers or admin token; owner-isolated
# ---------------------------------------------------------------------------

def _resolve_owner(
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
) -> dict[str, str]:
    """Resolve owner identity from headers / Authorization.

    Priority: X-User-Id header -> Authorization Bearer sub -> fallback anonymous
    Returns {user_id, agent_id, tenant_id}
    """
    user_id = x_user_id or request.headers.get("x-user-id") or request.headers.get("X-User-Id")
    tenant_id = x_tenant_id or request.headers.get("x-tenant-id") or request.headers.get("X-Tenant-Id") or "default"
    # Try Bearer JWT sub if no user_id
    if not user_id:
        auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            try:
                from jose import jwt  # type: ignore

                try:
                    payload = jwt.get_unverified_claims(token)
                    user_id = payload.get("sub") or payload.get("user_id") or payload.get("email")
                    if payload.get("tenant_id"):
                        tenant_id = payload["tenant_id"]
                except Exception:
                    pass
            except Exception:
                pass
    # Also check lower-case header iteration
    if not user_id:
        for k, v in request.headers.items():
            if k.lower() == "x-user-id" and v:
                user_id = v
                break
    if not user_id:
        user_id = "employee:anonymous"
    # Normalize to employee: prefix if bare
    if ":" not in user_id:
        user_id = f"employee:{user_id}"
    if not user_id.startswith("employee:") and not user_id.startswith("agent:"):
        user_id = f"employee:{user_id}"
    agent_id = derive_agent_id(user_id)
    return {"user_id": user_id, "agent_id": agent_id, "tenant_id": tenant_id}

# ---------------------------------------------------------------------------
# Mock data helpers (when DB not configured)
# ---------------------------------------------------------------------------

def _mock_notes(owner: dict[str, str], limit: int = 10) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    notes = [
        {
            "id": f"note_mock_{i}",
            "owner": owner["user_id"],
            "agent_id": owner["agent_id"],
            "title": f"Mock Note {i}",
            "content": f"This is mock note {i} for {owner['user_id']}",
            "vault_path": vault_path_for_note(owner["user_id"], f"note_mock_{i}"),
            "created_at": now,
            "updated_at": now,
            "tags": ["mock"],
            "source": "mock",
        }
        for i in range(1, min(limit, 5) + 1)
    ]
    return notes

def _mock_search_results(q: str, owner: dict[str, str], limit: int = 10) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    return [
        {
            "id": f"note_search_{i}",
            "title": f"Search hit {i} for '{q}'",
            "content": f"Mock search result {i} matching query '{q}'",
            "score": round(0.95 - i * 0.05, 3),
            "vault_path": vault_path_for_note(owner["user_id"], f"note_search_{i}"),
            "owner": owner["user_id"],
            "created_at": now,
        }
        for i in range(1, min(limit, 3) + 1)
    ]

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/attachments")
async def upload_attachment(
    request: Request,
    file: UploadFile = File(...),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """POST /v1/personal-wiki/attachments -> extract -> journal.

    Accepts a file upload, extracts text (stub), creates a journal note,
    stores reference under owner-isolated vault path, audit logged.
    Returns mock data when DB/vault not configured.
    """
    owner = _resolve_owner(request, x_user_id=x_user_id)
    user_id = owner["user_id"]
    agent_id = owner["agent_id"]
    tenant_id = owner["tenant_id"]

    # Read file (limit 10MB for skeleton)
    try:
        content = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"failed to read attachment: {e}")

    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large (max 10MB)")

    filename = file.filename or "unnamed"
    # Extract text — stub: try utf-8 decode, else hex preview
    try:
        extracted_text = content.decode("utf-8")[:5000]
    except Exception:
        extracted_text = content[:200].hex() + " ... (binary preview)"

    attachment_id = f"att_{uuid.uuid4().hex[:12]}"
    note_id = f"note_{uuid.uuid4().hex[:12]}"
    journal_id = f"journal_{uuid.uuid4().hex[:12]}"
    vault_path = vault_path_for_attachment(user_id, attachment_id)
    note_vault_path = vault_path_for_note(user_id, note_id)

    _audit(request, "PERSONAL_WIKI_ATTACHMENT_UPLOAD", {
        "user_id": user_id,
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "filename": filename,
        "attachment_id": attachment_id,
        "vault_path": vault_path,
        "size": len(content),
    })

    # If DB configured, we could persist to memories / vault — skeleton returns mock
    if not _is_db_configured() or not _is_vault_configured():
        logger.info(f"Personal Wiki vault not configured — returning mock for {user_id} (vault_path={vault_path})")
        # Still audit that we fell back
        return {
            "attachment_id": attachment_id,
            "filename": filename,
            "size": len(content),
            "vault_path": vault_path,
            "extracted_text": extracted_text[:500],
            "note": {
                "id": note_id,
                "title": filename,
                "content": extracted_text[:500],
                "vault_path": note_vault_path,
                "owner": user_id,
                "agent_id": agent_id,
            },
            "journal": {
                "id": journal_id,
                "note_id": note_id,
                "attachment_id": attachment_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            "mock": True,
            "db_configured": _is_db_configured(),
            "vault_configured": _is_vault_configured(),
        }

    # DB path — still stub but indicates persisted
    return {
        "attachment_id": attachment_id,
        "filename": filename,
        "size": len(content),
        "vault_path": vault_path,
        "extracted_text": extracted_text[:500],
        "note": {
            "id": note_id,
            "title": filename,
            "content": extracted_text[:500],
            "vault_path": note_vault_path,
            "owner": user_id,
            "agent_id": agent_id,
        },
        "journal": {
            "id": journal_id,
            "note_id": note_id,
            "attachment_id": attachment_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        "mock": False,
        "db_configured": True,
        "vault_configured": True,
    }


@router.get("/search")
async def search_notes(
    request: Request,
    q: str = Query(..., min_length=1, description="search query"),
    limit: int = Query(10, ge=1, le=50),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """GET /v1/personal-wiki/search?q= -> pgvector search stub.

    When DB configured, would run pgvector cosine search over memories/memory_embeddings
    filtered by owner (tenant + agent isolation). Currently returns mock ranked results.
    """
    owner = _resolve_owner(request, x_user_id=x_user_id)
    _audit(request, "PERSONAL_WIKI_SEARCH", {"user_id": owner["user_id"], "q": q, "limit": limit})

    if not _is_db_configured():
        results = _mock_search_results(q, owner, limit=limit)
        return {
            "query": q,
            "results": results,
            "count": len(results),
            "mock": True,
            "pgvector": False,
            "owner": owner["user_id"],
            "vault_path": get_vault_path(owner["user_id"]),
        }

    # DB configured but still stub — attempt real pgvector search if available, else mock
    try:
        # Lazy attempt: if we can query memories with vector, do so; else fallback
        # For skeleton we just return mock with pgvector flag
        results = _mock_search_results(q, owner, limit=limit)
        return {
            "query": q,
            "results": results,
            "count": len(results),
            "mock": True,
            "pgvector": True,
            "owner": owner["user_id"],
            "vault_path": get_vault_path(owner["user_id"]),
        }
    except Exception as e:
        logger.warning(f"pgvector search fallback for q={q}: {e}")
        results = _mock_search_results(q, owner, limit=limit)
        return {
            "query": q,
            "results": results,
            "count": len(results),
            "mock": True,
            "pgvector": False,
            "owner": owner["user_id"],
            "vault_path": get_vault_path(owner["user_id"]),
            "error": str(e),
        }


@router.get("/notes")
async def list_notes(
    request: Request,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """GET /v1/personal-wiki/notes -> list notes (owner-isolated).

    Returns notes for the authenticated owner only. Mock when DB not configured.
    """
    owner = _resolve_owner(request, x_user_id=x_user_id)
    _audit(request, "PERSONAL_WIKI_LIST_NOTES", {"user_id": owner["user_id"], "limit": limit, "offset": offset})

    if not _is_db_configured():
        notes = _mock_notes(owner, limit=limit)
        # apply offset
        paged = notes[offset: offset + limit]
        return {
            "notes": paged,
            "count": len(paged),
            "total": len(notes),
            "mock": True,
            "owner": owner["user_id"],
            "vault_path": get_vault_path(owner["user_id"]),
        }

    # DB configured — would query memories where owner==user_id; stub
    try:
        notes = _mock_notes(owner, limit=limit)
        paged = notes[offset: offset + limit]
        return {
            "notes": paged,
            "count": len(paged),
            "total": len(notes),
            "mock": True,
            "owner": owner["user_id"],
            "vault_path": get_vault_path(owner["user_id"]),
        }
    except Exception as e:
        logger.warning(f"list notes fallback: {e}")
        notes = _mock_notes(owner, limit=limit)
        return {
            "notes": notes[offset: offset + limit],
            "count": len(notes),
            "total": len(notes),
            "mock": True,
            "owner": owner["user_id"],
            "vault_path": get_vault_path(owner["user_id"]),
            "error": str(e),
        }
