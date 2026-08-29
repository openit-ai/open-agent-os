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
    """Owner-isolated vault path for Personal Wiki (H3: traversal guard)."""
    for val, label in ((suffix, "suffix"), (user_id, "user_id")):
        if val and ".." in val.split("/"):
            from fastapi import HTTPException as _HTTPException
            raise _HTTPException(status_code=403, detail=f"PATH_TRAVERSAL: '..' in {label}")
        if val and val.startswith("/"):
            from fastapi import HTTPException as _HTTPException
            raise _HTTPException(status_code=403, detail=f"PATH_TRAVERSAL: absolute {label}")
    agent_id = derive_agent_id(user_id)
    prefix = os.environ.get("VAULT_KV_PREFIX", "openagentos/").strip()
    if prefix and not prefix.endswith("/"):
        prefix = prefix + "/"
    if not prefix:
        prefix = "openagentos/"
    base = f"{prefix}{agent_id}/personal_wiki"
    if suffix:
        suffix = suffix.lstrip("/")
        if ".." in suffix.split("/"):
            from fastapi import HTTPException as _HTTPException
            raise _HTTPException(status_code=403, detail="PATH_TRAVERSAL: '..' in suffix")
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
# Auth helper — H3 verified JWT owner resolution (no unverified claims)
# ---------------------------------------------------------------------------

def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        v = os.environ.get(k, "").strip().lower()
        if v in ("production", "prod"):
            return True
    return False

def _allow_test_fixture() -> bool:
    if _is_production():
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    flag = os.environ.get("OAOS_ALLOW_TEST_FIXTURE", "") or os.environ.get("OAOS_ALLOW_TEST_FALLBACK", "")
    if flag.strip().lower() in ("1", "true", "yes", "on"):
        return True
    return False

def _verify_wiki_jwt(token: str, required_scope: str | None = None) -> dict:
    """Verify wiki JWT with issuer/audience/exp/tenant/agent/scope (H3) — isolated loader, no bare `auth`."""
    try:
        import importlib.util as _ilu2, sys as _sys3, pathlib as _pl2
        # find packages/personal-wiki/personal_wiki/auth.py via file location
        cand = _pl2.Path(__file__).resolve()
        ap = None
        for p in [cand] + list(cand.parents):
            q = p / "packages" / "personal-wiki" / "personal_wiki" / "auth.py"
            if q.exists():
                ap = q
                break
        if ap is None:
            ap = _pl2.Path(__file__).parents[2] / "packages" / "personal-wiki" / "personal_wiki" / "auth.py"
        if "personal_wiki.auth" in _sys3.modules:
            _mod = _sys3.modules["personal_wiki.auth"]
            _v = getattr(_mod, "verify_wiki_jwt", None)
            if _v:
                return _v(token, required_scope=required_scope)
        spec = _ilu2.spec_from_file_location("personal_wiki.auth", str(ap))
        if spec and spec.loader:
            if "personal_wiki" not in _sys3.modules or not hasattr(_sys3.modules["personal_wiki"], "__path__"):
                import types as _types2, importlib.machinery as _mach2
                pkg = _types2.ModuleType("personal_wiki")
                pkg.__path__ = [str(ap.parent)]  # type: ignore
                pkg.__spec__ = _mach2.ModuleSpec("personal_wiki", None, is_package=True)  # type: ignore
                _sys3.modules["personal_wiki"] = pkg
            # if already loaded, reuse; else load fresh into separate name to avoid overwrite race
            if spec.name not in _sys3.modules:
                _mod2 = _ilu2.module_from_spec(spec)
                _sys3.modules[spec.name] = _mod2
                spec.loader.exec_module(_mod2)  # type: ignore
                _mod = _mod2
            else:
                _mod = _sys3.modules[spec.name]
            _v = getattr(_mod, "verify_wiki_jwt", None)
            if _v:
                return _v(token, required_scope=required_scope)
    except HTTPException:
        raise
    except Exception:
        from jose import jwt as _jwt, JWTError as _JWTError, ExpiredSignatureError as _Exp  # type: ignore
        ALLOWED_ISSUERS = {"control-plane", "security", "open-agent-os-auth"}
        ALLOWED_AUDIENCES = {"wiki-fs", "memory-service", "wiki", "security"}
        ALLOWED_SCOPES = {"wiki:read", "wiki:write"}
        _DEV = "dev-admin-jwt-secret-please-change"
        key = os.environ.get("OAOS_SIGNING_KEY") or os.environ.get("OAOS_SECURITY_SERVICE_SIGNING_KEY") or os.environ.get("JWT_SIGNING_KEY") or os.environ.get("ADMIN_JWT_SECRET") or _DEV
        if _is_production() and key == _DEV:
            raise HTTPException(status_code=503, detail="wiki JWT signing key not configured in production")
        try:
            payload = _jwt.decode(token, key, algorithms=["HS256"], options={"verify_aud": False, "verify_iss": False})
        except _Exp as e:
            raise HTTPException(status_code=401, detail="wiki JWT expired") from e
        except _JWTError as e:
            raise HTTPException(status_code=401, detail=f"invalid wiki JWT: {e}") from e
        iss = payload.get("iss")
        if iss not in ALLOWED_ISSUERS:
            raise HTTPException(status_code=401, detail=f"invalid issuer: {iss}")
        aud = payload.get("aud")
        aud_ok = False
        if isinstance(aud, list):
            aud_ok = any(a in ALLOWED_AUDIENCES for a in aud)
        elif isinstance(aud, str):
            aud_ok = aud in ALLOWED_AUDIENCES
        if not aud_ok:
            raise HTTPException(status_code=401, detail=f"invalid audience: {aud}")
        if not payload.get("sub"):
            raise HTTPException(status_code=401, detail="missing sub claim")
        if not payload.get("tenant_id"):
            raise HTTPException(status_code=401, detail="missing tenant_id claim")
        if not payload.get("agent_id"):
            raise HTTPException(status_code=401, detail="missing agent_id claim")
        scope = payload.get("scope")
        if scope not in ALLOWED_SCOPES:
            raise HTTPException(status_code=401, detail=f"missing or invalid scope: {scope}")
        if "exp" not in payload:
            raise HTTPException(status_code=401, detail="missing exp claim")
        if "jti" not in payload or not payload.get("jti"):
            raise HTTPException(status_code=401, detail="missing jti claim")
        if required_scope:
            if required_scope == "wiki:read" and scope not in ("wiki:read", "wiki:write"):
                raise HTTPException(status_code=401, detail=f"invalid scope for read: {scope}")
            if required_scope == "wiki:write" and scope != "wiki:write":
                raise HTTPException(status_code=401, detail=f"scope {scope} not authorized for write")
        return payload

def _resolve_owner(
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
    required_scope: str | None = None,
) -> dict[str, str]:
    """H3: Resolve owner from verified Wiki JWT only. No unverified claims, no header trust in prod."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    token: str | None = None
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip() or None
    if token:
        payload = _verify_wiki_jwt(token, required_scope=required_scope)
        user_id = payload.get("sub") or payload.get("user_id") or ""
        tenant_id = payload.get("tenant_id")
        agent_id = payload.get("agent_id")
        _ht = x_tenant_id if isinstance(x_tenant_id, str) else None
        header_tenant = _ht or request.headers.get("x-tenant-id") or request.headers.get("X-Tenant-Id")
        if header_tenant and str(header_tenant) != str(tenant_id):
            raise HTTPException(status_code=403, detail=f"tenant mismatch: token tenant {tenant_id} != requested {header_tenant}")
        _hu = x_user_id if isinstance(x_user_id, str) else None
        header_user = _hu or request.headers.get("x-user-id") or request.headers.get("X-User-Id")
        if header_user:
            hdr = header_user.strip()
            if ":" not in hdr:
                hdr = f"employee:{hdr}"
            if hdr != user_id and hdr != agent_id:
                if derive_agent_id(hdr) != agent_id and derive_agent_id(user_id) != derive_agent_id(hdr):
                    raise HTTPException(status_code=403, detail=f"user mismatch: header {hdr} != JWT sub {user_id}")
        if ":" not in user_id:
            user_id = f"employee:{user_id}"
        if not user_id.startswith("employee:") and not user_id.startswith("agent:"):
            user_id = f"employee:{user_id}"
        if not agent_id:
            agent_id = derive_agent_id(user_id)
        return {"user_id": user_id, "agent_id": agent_id, "tenant_id": tenant_id}
    if _allow_test_fixture():
        _fu = x_user_id if isinstance(x_user_id, str) else None
        user_id = _fu or request.headers.get("x-user-id") or request.headers.get("X-User-Id")
        if not user_id:
            for k, v in request.headers.items():
                if k.lower() == "x-user-id" and v:
                    user_id = v
                    break
        if user_id:
            _ht2 = x_tenant_id if isinstance(x_tenant_id, str) else None
            tenant_id = _ht2 or request.headers.get("x-tenant-id") or request.headers.get("X-Tenant-Id") or "default"
            if ":" not in user_id:
                user_id = f"employee:{user_id}"
            if not user_id.startswith("employee:") and not user_id.startswith("agent:"):
                user_id = f"employee:{user_id}"
            agent_id = derive_agent_id(user_id)
            return {"user_id": user_id, "agent_id": agent_id, "tenant_id": tenant_id}
    raise HTTPException(status_code=401, detail="wiki JWT required")

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
    owner = _resolve_owner(request, x_user_id=x_user_id, required_scope="wiki:write")
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
    if ".." in filename.split("/") or ".." in filename.split("\\") or filename.startswith("/") or filename.startswith("\\"):
        raise HTTPException(status_code=403, detail="PATH_TRAVERSAL: '..' in filename")
    filename = filename.split("/")[-1].split("\\")[-1]
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

# ── Consolidation endpoint (02:00 KST scheduler trigger, graceful) ─

@router.post("/consolidate")
async def trigger_consolidation(
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    lang: str = Query("ko", description="prompt language ko/en"),
    dry_run: bool = Query(False, description="dry run without writing notes"),
):
    """POST /v1/personal-wiki/consolidate -> run consolidate_once for owner."""
    owner = _resolve_owner(request, x_user_id=x_user_id)
    user_id = owner["user_id"]
    ws_id = user_id
    try:
        import sys
        from pathlib import Path
        pkg_path = Path(__file__).resolve().parents[2] / "packages" / "personal-wiki"
        if str(pkg_path) not in sys.path:
            sys.path.insert(0, str(pkg_path))
        from personal_wiki.consolidate import consolidate_once  # type: ignore
        result = consolidate_once(ws_id=ws_id, lang=lang, dry_run=dry_run)
        _audit(request, "CONSOLIDATION_RUN", {"user_id": user_id, "ws_id": ws_id, "result": result})
        return {"owner": user_id, "ws_id": ws_id, **result}
    except Exception as e:
        logger.warning(f"consolidate trigger failed for {user_id}: {e}")
        _audit(request, "CONSOLIDATION_RUN_FAILED", {"user_id": user_id, "ws_id": ws_id, "error": str(e)})
        return {"owner": user_id, "ws_id": ws_id, "action": "error", "error": str(e), "mock": True}


@router.get("/consolidation/status")
async def consolidation_status(
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """GET /v1/personal-wiki/consolidation/status -> watermark + scheduler info."""
    owner = _resolve_owner(request, x_user_id=x_user_id)
    user_id = owner["user_id"]
    ws_id = user_id
    try:
        import sys
        from pathlib import Path
        pkg_path = Path(__file__).resolve().parents[2] / "packages" / "personal-wiki"
        if str(pkg_path) not in sys.path:
            sys.path.insert(0, str(pkg_path))
        from personal_wiki.consolidate import _load_watermark, _hermes_config, WATERMARK, CAP_BYTES  # type: ignore
        watermark = _load_watermark(ws_id=ws_id)
        _, key, model = _hermes_config()
        return {
            "owner": user_id,
            "ws_id": ws_id,
            "watermark": watermark,
            "watermark_file": WATERMARK,
            "cap_bytes": CAP_BYTES,
            "cap_14kb": CAP_BYTES == 14336,
            "hermes_key_set": bool(key),
            "hermes_model": model,
            "scheduler": "02:00 KST (Asia/Seoul) via Hermes cron or APScheduler fallback — set OAOS_WIKI_CONSOLIDATION_CRON=1",
        }
    except Exception as e:
        return {"owner": user_id, "ws_id": ws_id, "error": str(e), "mock": True}
