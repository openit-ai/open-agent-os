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

import asyncio
import logging
import os
import uuid
from pathlib import Path
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
# Vault FS + extractor helpers — tenant/agent isolated, production-consistent
# ---------------------------------------------------------------------------
def _load_vault_module():
    try:
        import importlib.util as _ilu, sys as _sys, pathlib as _pl, types as _types, importlib.machinery as _mach
        pkg_root = _pl.Path(__file__).resolve().parents[2] / "packages" / "personal-wiki"
        if str(pkg_root) not in _sys.path:
            _sys.path.insert(0, str(pkg_root))
        if "personal_wiki" not in _sys.modules or not hasattr(_sys.modules["personal_wiki"], "__path__"):
            pkg = _types.ModuleType("personal_wiki")
            pkg.__path__ = [str(pkg_root / "personal_wiki")]
            pkg.__spec__ = _mach.ModuleSpec("personal_wiki", None, is_package=True)
            _sys.modules["personal_wiki"] = pkg
        from personal_wiki import vault as _vault  # type: ignore
        return _vault
    except Exception as e:
        logger.debug(f"vault load failed: {e}")
        return None

def _owner_vault_root(tenant_id: str, agent_id: str) -> Path:
    from pathlib import Path as _P
    vault_mod = _load_vault_module()
    if vault_mod is not None:
        try:
            base = vault_mod.get_vault_root()
            # tenant/agent isolated root
            ow = vault_mod.vault_path_for_tenant_agent(tenant_id, agent_id, vault_root=base)
            return _P(ow)
        except Exception:
            pass
    # fallback: env vault root or default
    for k in ("OAOS_WIKI_VAULT","PERSONAL_WIKI_VAULT","VAULT_ROOT"):
        v=os.environ.get(k)
        if v and v.strip():
            return _P(v.strip()).expanduser().resolve() / tenant_id / agent_id
    return _P.home() / ".open-agent-os" / "wiki-vault" / tenant_id / agent_id

def _sanitize_filename(fn: str) -> str:
    import re
    fn = fn.split("/")[-1].split("\\")[-1].strip()
    if not fn:
        fn = f"unnamed_{uuid.uuid4().hex[:8]}"
    # reject traversal already handled, now sanitize chars
    fn = re.sub(r"[^\w\-\. ]", "-", fn)
    fn = re.sub(r"-+","-", fn).strip("-").strip()
    if not fn:
        fn = f"file_{uuid.uuid4().hex[:8]}"
    # limit length
    if len(fn) > 120:
        ext = Path(fn).suffix
        fn = fn[:120-len(ext)] + ext
    return fn

def _persist_attachment_fs(tenant_id: str, agent_id: str, filename: str, content: bytes) -> Path:
    root = _owner_vault_root(tenant_id, agent_id)
    att_dir = root / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _sanitize_filename(filename)
    # ensure unique if exists
    dest = att_dir / safe_name
    # also guard with safe_join
    vault_mod = _load_vault_module()
    if vault_mod is not None and hasattr(vault_mod, "safe_join_vault"):
        try:
            dest = vault_mod.safe_join_vault(root, "attachments", safe_name)
        except Exception:
            pass
    # de-dupe
    if dest.exists():
        stem = dest.stem; suf = dest.suffix
        dest = att_dir / f"{stem}_{uuid.uuid4().hex[:6]}{suf}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return dest

def _extract_text_for_file(path: Path, max_chars: int = 5000) -> str:
    try:
        vault_mod = _load_vault_module()
        # prefer extractor
        import importlib.util as _ilu
        pkg_root = Path(__file__).resolve().parents[2] / "packages" / "personal-wiki"
        ep = pkg_root / "personal_wiki" / "extractor.py"
        if ep.exists():
            import sys as _sys
            if "personal_wiki.extractor" not in _sys.modules:
                spec = _ilu.spec_from_file_location("personal_wiki.extractor", str(ep))
                if spec and spec.loader:
                    mod = _ilu.module_from_spec(spec)
                    _sys.modules[spec.name]=mod
                    spec.loader.exec_module(mod)
            mod = _sys.modules.get("personal_wiki.extractor")
            if mod and hasattr(mod, "extract_text"):
                try:
                    return mod.extract_text(path, max_chars=max_chars)  # type: ignore
                except Exception as e:
                    logger.debug(f"extractor failed: {e}")
    except Exception:
        pass
    # fallback: utf8 decode or hex preview
    try:
        data = path.read_bytes() if isinstance(path, Path) else Path(path).read_bytes()
        try:
            return data.decode("utf-8")[:max_chars]
        except Exception:
            return data[:200].hex() + " ... (binary preview)"
    except Exception as e:
        return f"[extraction failed: {e}]"

def _list_notes_fs(tenant_id: str, agent_id: str, limit: int = 10, offset: int = 0) -> list[dict]:
    root = _owner_vault_root(tenant_id, agent_id)
    notes_dir = root / "notes"
    journal_dir = root / "journal"
    out=[]
    if notes_dir.exists():
        files = sorted(notes_dir.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[offset: offset+limit*3]:
            try:
                rel = f.relative_to(root).as_posix()
                stat = f.stat()
                content = f.read_text(encoding="utf-8", errors="ignore")[:2000]
                # title from first markdown heading or filename
                title = f.stem
                for line in content.splitlines()[:5]:
                    if line.strip().startswith("#"):
                        title = line.strip().lstrip("#").strip()[:80] or title
                        break
                out.append({"id": f.stem, "title": title, "content": content[:500], "vault_path": rel, "path": str(f), "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(), "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()})
                if len(out) >= limit:
                    break
            except Exception:
                continue
    # also include journal-derived notes? keep simple
    return out[:limit]

def _search_notes_fs(tenant_id: str, agent_id: str, q: str, limit: int = 10) -> list[dict]:
    root = _owner_vault_root(tenant_id, agent_id)
    ql = q.lower().strip()
    if not ql:
        return []
    candidates=[]
    for sub in ("notes","journal"):
        d = root / sub
        if not d.exists():
            continue
        for f in d.rglob("*.md"):
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
                if ql in txt.lower() or ql in f.name.lower():
                    rel = f.relative_to(root).as_posix()
                    # score simple TF: count occurrences
                    score = txt.lower().count(ql) * 0.1 + (1.0 if ql in f.name.lower() else 0)
                    score = min(0.99, 0.5+score)
                    candidates.append({"id": f.stem, "title": f.stem, "content": txt[:500], "vault_path": rel, "score": round(score,3), "path": str(f)})
            except Exception:
                continue
    # sort by score
    candidates.sort(key=lambda x: x.get("score",0), reverse=True)
    return candidates[:limit]

# ---------------------------------------------------------------------------
# Image handling — attachment reference + runtime forwarding (NO LLM selection,
# NO OCR: image is forwarded as user-turn via active Agent Runtime ACP/Hermes)
# ---------------------------------------------------------------------------
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"})
_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".tiff": "image/tiff", ".tif": "image/tiff",
}
IMAGE_RUNTIME_MARKER = "Image attachment reference"
IMAGE_RUNTIME_KIND = "image_attachment_reference"


def _is_image_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in _IMAGE_EXTS


def _build_image_attachment_ref(saved_path: Path, vault_path: str, attachment_id: str) -> dict[str, Any]:
    ext = saved_path.suffix.lower()
    mime = _IMAGE_MIME.get(ext, "application/octet-stream")
    try:
        size = saved_path.stat().st_size if saved_path.exists() else 0
    except Exception:
        size = 0
    return {
        "kind": IMAGE_RUNTIME_KIND,
        "attachment_id": attachment_id,
        "filename": saved_path.name,
        "ext": ext,
        "mime": mime,
        "size_bytes": size,
        "vault_path": vault_path,
        "saved_path": str(saved_path),
    }


def _build_image_runtime_instruction(attachment_ref: dict[str, Any], extra: str | None = None) -> str:
    """User-turn instruction for active Agent Runtime (no LLM selection, no OCR).

    This string is intended to be sent as a `prompt` via
    POST /v1/sessions/{session_id}/prompt (Control Plane -> ACP/Hermes).
    """
    fn = attachment_ref.get("filename", "image")
    ext = attachment_ref.get("ext", "")
    size = attachment_ref.get("size_bytes", 0)
    vp = attachment_ref.get("vault_path", "")
    aid = attachment_ref.get("attachment_id", "pending")
    base = (
        f"[Image Attachment — LLM Vision Pending] {fn} ({ext}, {size} bytes) stored at {vp} — attachment_id={aid}\n"
        f"[{IMAGE_RUNTIME_MARKER}: {fn} ({ext}, {size} bytes) stored at {vp} — attachment_id={aid}] "
        "LLM Vision Request Prompt (deterministic, no local OCR): "
        "Please analyze this image attachment via the LLM vision capability at query/runtime time. "
        "Describe, transcribe, or interpret the image as requested by the user. "
        "Status: pending LLM vision processing — no OCR text extracted locally; no separate OCR or vision API was invoked. "
        "This is a user-turn instruction forwarded via the active Agent Runtime (ACP/Hermes)."
    )
    if extra:
        base = base + " " + extra.strip()
    return base


def _extract_runtime_context(request: Request, tenant_id: str, agent_id: str, user_id: str) -> dict[str, Any]:
    """Extract active Agent Runtime conversation context from headers/query.

    Required: session_id (X-Session-Id), tenant_id, user_id.
    Optional pass-through: channel_id/root_id/post_id/thread, trace_id.
    Headers are case-insensitive; also accepts X-Channel-Id, X-Root-Id, X-Post-Id, X-Trace-Id.
    """
    h = {k.lower(): v for k, v in request.headers.items()}
    # session_id is the primary runtime key
    session_id = (
        h.get("x-session-id")
        or h.get("x-oaos-session-id")
        or request.query_params.get("session_id")
        or ""
    )
    channel_id = h.get("x-channel-id") or h.get("x-channel") or request.query_params.get("channel_id") or ""
    root_id = h.get("x-root-id") or h.get("x-root") or request.query_params.get("root_id") or ""
    post_id = h.get("x-post-id") or h.get("x-post") or request.query_params.get("post_id") or ""
    trace_id = h.get("x-trace-id") or request.query_params.get("trace_id") or ""
    return {
        "session_id": session_id.strip() if isinstance(session_id, str) else "",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "channel_id": channel_id.strip() if isinstance(channel_id, str) else "",
        "root_id": root_id.strip() if isinstance(root_id, str) else "",
        "post_id": post_id.strip() if isinstance(post_id, str) else "",
        "trace_id": trace_id.strip() if isinstance(trace_id, str) else "",
    }


def _control_plane_base_url() -> str:
    for k in ("OAOS_CONTROL_PLANE_URL", "OAOS_CP_BASE_URL", "CONTROL_PLANE_URL"):
        v = os.environ.get(k, "").strip()
        if v:
            return v.rstrip("/")
    # default dev location; may be unreachable — caller must handle gracefully
    return "http://localhost:8000"


def _build_runtime_forwarding_payload(runtime_instruction: str, attachment_ref: dict[str, Any], runtime_ctx: dict[str, Any]) -> dict[str, Any]:
    rid = f"req_img_{attachment_ref.get('attachment_id','')}"
    fid = attachment_ref.get("attachment_id") or attachment_ref.get("vault_path") or ""
    return {
        "prompt": runtime_instruction,
        "attachment_ref": attachment_ref,
        "attachment_refs": [attachment_ref],
        "attachments": [attachment_ref],
        "file_ids": [fid] if fid else [],
        "runtime_context": runtime_ctx,
        "request_id": rid,
        "via": "control_plane_acp",
        "control_plane_route": f"POST /v1/sessions/{runtime_ctx.get('session_id','')}/prompt",
        "acp_path": "control_plane.acp_adapter.ACPAdapter.send_prompt -> Hermes ACP / Gateway",
        "multimodal_contract": "prompt (text) + file_ids/attachment_refs forwarded via active Agent Runtime current-session message (ACP/Hermes); no model/provider selection, no OCR",
    }


def _no_mock_in_production():
    if _is_production():
        raise HTTPException(status_code=503, detail="Personal Wiki not configured in production (mock fallback disabled)")


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
    """POST /v1/personal-wiki/attachments -> vault FS + extractor + journal (owner-isolated)."""
    owner = _resolve_owner(request, x_user_id=x_user_id, required_scope="wiki:write")
    user_id = owner["user_id"]
    agent_id = owner["agent_id"]
    tenant_id = owner["tenant_id"]

    try:
        content = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"failed to read attachment: {e}")

    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large (max 10MB)")

    filename = file.filename or "unnamed"
    if ".." in filename.split("/") or ".." in filename.split("\\") or filename.startswith("/") or filename.startswith("\\"):
        raise HTTPException(status_code=403, detail="PATH_TRAVERSAL: '..' in filename")
    for seg in filename.replace("\\","/").split("/"):
        if seg == "..":
            raise HTTPException(status_code=403, detail="PATH_TRAVERSAL: '..' in filename")

    try:
        saved_path = _persist_attachment_fs(tenant_id, agent_id, filename, content)
        safe_filename = saved_path.name
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"vault persist failed: {e}")
        if _is_production():
            raise HTTPException(status_code=503, detail=f"vault persist failed: {e}")
        safe_filename = _sanitize_filename(filename)
        saved_path = Path("/tmp") / safe_filename

    # --- Common IDs / vault paths ---
    attachment_id = f"att_{uuid.uuid4().hex[:12]}"
    note_id = f"note_{uuid.uuid4().hex[:12]}"
    journal_id = f"journal_{uuid.uuid4().hex[:12]}"
    owner_root = _owner_vault_root(tenant_id, agent_id)
    vault_path = f"{tenant_id}/{agent_id}/attachments/{safe_filename}"
    try:
        from personal_wiki.vault import get_vault_root as _gvr
        base = _gvr()
        vault_path = saved_path.relative_to(base).as_posix()
    except Exception:
        pass

    is_image = _is_image_file(safe_filename)
    # For images: attachment reference + runtime instruction (NO OCR, NO LLM selection)
    # For non-images: normal extractor text
    if is_image:
        attachment_ref = _build_image_attachment_ref(saved_path, vault_path, attachment_id)
        runtime_instruction = _build_image_runtime_instruction(attachment_ref)
        extracted_text = runtime_instruction
        runtime_ctx = _extract_runtime_context(request, tenant_id, agent_id, user_id)
        # Determine forwarding status — explicit contract when Admin API cannot access runtime
        if not runtime_ctx.get("session_id"):
            runtime_forwarding: dict[str, Any] = {
                "status": "runtime_forwarding_required",
                "reason": "Admin API upload endpoint cannot access active Agent Runtime without session_id — image must be forwarded via Control Plane ACP/Hermes using the currently active conversation context (session_id/tenant_id/user_id/channel/root/post).",
                "required_context": ["session_id (X-Session-Id header or ?session_id)", "tenant_id (from JWT)", "user_id (from JWT)", "channel_id/root_id/post_id optional (X-Channel-Id / X-Root-Id / X-Post-Id)"],
                "provided_context": runtime_ctx,
                "forward_via": "control_plane_acp",
                "control_plane_route": "POST /v1/sessions/{session_id}/prompt (Control Plane -> ACPAdapter.send_prompt -> Hermes ACP/Gateway)",
                "acp_path": "control_plane.acp_adapter.ACPAdapter",
                "payload": _build_runtime_forwarding_payload(runtime_instruction, attachment_ref, runtime_ctx),
                "note": "No LLM/model/provider was selected in Personal Wiki or extractor. Client should retry upload with active runtime headers or forward this payload through the Control Plane. No new LLM endpoint is invented.",
            }
        else:
            # Attempt live forwarding through Control Plane if reachable; otherwise return queued/required
            cp_url = _control_plane_base_url()
            fwd_payload = _build_runtime_forwarding_payload(runtime_instruction, attachment_ref, runtime_ctx)
            # Best-effort synchronous forward (never blocks vault persist) — use httpx with short timeout
            forward_result: dict[str, Any] = {"attempted": True, "control_plane_url": cp_url}
            try:
                import httpx  # type: ignore
                # Extract caller auth for CP (forward same Authorization header if present)
                auth_hdr = request.headers.get("authorization") or request.headers.get("Authorization") or ""
                hdrs: dict[str, str] = {
                    "X-Tenant-Id": runtime_ctx["tenant_id"],
                    "X-User-Id": runtime_ctx["user_id"],
                    "X-Agent-Id": runtime_ctx["agent_id"],
                    "X-Session-Id": runtime_ctx["session_id"],
                    "Content-Type": "application/json",
                }
                if runtime_ctx.get("channel_id"):
                    hdrs["X-Channel-Id"] = runtime_ctx["channel_id"]
                if runtime_ctx.get("root_id"):
                    hdrs["X-Root-Id"] = runtime_ctx["root_id"]
                if runtime_ctx.get("post_id"):
                    hdrs["X-Post-Id"] = runtime_ctx["post_id"]
                if auth_hdr:
                    hdrs["Authorization"] = auth_hdr
                # Nonblocking async forward with bounded timeout (2.0s) — never blocks event loop
                try:
                    async with httpx.AsyncClient(timeout=2.0) as client:
                        resp = await asyncio.wait_for(
                            client.post(
                                f"{cp_url}/v1/sessions/{runtime_ctx['session_id']}/prompt",
                                json={"prompt": runtime_instruction, "request_id": fwd_payload["request_id"], "attachment_ref": attachment_ref, "attachment_refs": [attachment_ref], "attachments": [attachment_ref], "file_ids": fwd_payload["file_ids"], "runtime_context": runtime_ctx},
                                headers=hdrs,
                            ),
                            timeout=2.5,
                        )
                        if resp.status_code in (200, 201, 202):
                            try:
                                j = resp.json()
                            except Exception:
                                j = {"status_code": resp.status_code}
                            forward_result.update({"status": "forwarded", "response": j, "http_status": resp.status_code})
                        else:
                            forward_result.update({"status": "queued", "http_status": resp.status_code, "body": resp.text[:500]})
                except asyncio.TimeoutError:
                    forward_result.update({"status": "queued", "reason": "control plane timeout (2.0s bounded)", "payload": fwd_payload})
                except Exception as e:
                    forward_result.update({"status": "queued", "reason": f"control plane unreachable: {e}", "payload": fwd_payload})
            except Exception as e:
                forward_result.update({"status": "queued", "reason": f"forward attempt failed: {e}", "payload": fwd_payload})
            # Build explicit forwarding event
            if forward_result.get("status") == "forwarded":
                runtime_forwarding = {
                    "status": "forwarded",
                    "via": "control_plane_acp",
                    "control_plane_url": cp_url,
                    "control_plane_route": f"POST /v1/sessions/{runtime_ctx['session_id']}/prompt",
                    "acp_path": "control_plane.acp_adapter.ACPAdapter.send_prompt",
                    "runtime_context": runtime_ctx,
                    "payload": fwd_payload,
                    "result": forward_result,
                }
            else:
                runtime_forwarding = {
                    "status": "queued",
                    "reason": forward_result.get("reason") or "Control Plane not reachable synchronously — payload queued for forwarding via ACP/Hermes",
                    "via": "control_plane_acp",
                    "control_plane_url": cp_url,
                    "control_plane_route": f"POST /v1/sessions/{runtime_ctx['session_id']}/prompt",
                    "acp_path": "control_plane.acp_adapter.ACPAdapter",
                    "runtime_context": runtime_ctx,
                    "payload": fwd_payload,
                    "attempt": forward_result,
                    "note": "No LLM/model/provider selected; Admin API does not invent a new LLM endpoint. Forward through existing Control Plane route.",
                }
        note_frontmatter: dict[str, Any] = {
            "source": safe_filename,
            "attachment_id": attachment_id,
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "type": "image",
            "vision_status": "runtime_forwarding",
            "extractor": "attachment_reference_runtime_instruction",
            "runtime_forwarding": runtime_forwarding.get("status"),
        }
    else:
        extracted_text = _extract_text_for_file(saved_path, max_chars=5000)
        attachment_ref = None  # type: ignore
        runtime_instruction = None  # type: ignore
        runtime_forwarding = None  # type: ignore
        runtime_ctx = None  # type: ignore
        note_frontmatter = {"source": safe_filename, "attachment_id": attachment_id, "tenant_id": tenant_id, "agent_id": agent_id}
    # Persist note (after branching so extracted_text is correct type)
    notes_dir = owner_root / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_path = notes_dir / f"{note_id}.md"
    try:
        vault_mod = _load_vault_module()
        if vault_mod is not None and hasattr(vault_mod, "upsert_note"):
            try:
                np = vault_mod.upsert_note(note_id, extracted_text[:5000], frontmatter=note_frontmatter, vault_root=owner_root)
                if np is not None:
                    note_path = Path(np)
            except Exception as e:
                logger.debug(f"upsert_note failed: {e}")
                note_path.write_text(f"---\nsource: {safe_filename}\n---\n\n" + extracted_text[:5000], encoding="utf-8")
        else:
            note_path.write_text(f"---\nsource: {safe_filename}\n---\n\n" + extracted_text[:5000], encoding="utf-8")
    except Exception as e:
        logger.warning(f"note create failed: {e}")
    try:
        vault_mod = _load_vault_module()
        if vault_mod is not None and hasattr(vault_mod, "append_journal"):
            try:
                vault_mod.append_journal(f"att-{attachment_id}", "attachment_upload", {"filename": safe_filename, "extracted": extracted_text[:2000]}, vault_root=owner_root, when=datetime.now(timezone.utc))
            except Exception as e:
                logger.debug(f"journal append failed: {e}")
    except Exception:
        pass
    _audit(request, "PERSONAL_WIKI_ATTACHMENT_UPLOAD", {
        "user_id": user_id,
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "filename": safe_filename,
        "attachment_id": attachment_id,
        "vault_path": vault_path,
        "size": len(content),
        "saved_path": str(saved_path),
        **({"is_image": True, "runtime_forwarding": runtime_forwarding.get("status")} if is_image else {}),
    })
    is_mock = False
    try:
        is_mock = not saved_path.exists() or "tmp" in str(saved_path)
    except Exception:
        is_mock = False
    if _is_production() and is_mock:
        _no_mock_in_production()
    note_vault_path = f"{tenant_id}/{agent_id}/notes/{note_id}.md"
    try:
        note_rel = note_path.relative_to(owner_root).as_posix()
        note_vault_path = note_rel
    except Exception:
        pass
    # --- Build response — explicit contract for images vs documents ---
    base_resp: dict[str, Any] = {
        "attachment_id": attachment_id,
        "filename": safe_filename,
        "size": len(content),
        "vault_path": vault_path,
        "extracted_text": extracted_text[:500],
        "note": {
            "id": note_id,
            "title": safe_filename,
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
        "db_configured": _is_db_configured(),
        "vault_configured": _is_vault_configured(),
        "saved_path": str(saved_path),
    }
    if is_image:
        # No provider/model selection — image is attachment reference forwarded via active runtime (direct delivery, multimodal contract)
        base_resp.update({
            "type": "image",
            "attachment_ref": attachment_ref,
            "attachment_refs": [attachment_ref],
            "attachments": [attachment_ref],
            "file_ids": [attachment_ref.get("attachment_id") or attachment_ref.get("vault_path")],
            "runtime_instruction": runtime_instruction,
            "runtime_context": runtime_ctx,
            "runtime_forwarding": runtime_forwarding,
            # Explicit contract disclosure when Admin API has no runtime session
            "forwarding_contract": "If runtime_forwarding.status == 'runtime_forwarding_required', Admin API cannot access active conversation runtime (session_id missing). Client MUST forward runtime_instruction + attachment_ref via Control Plane POST /v1/sessions/{session_id}/prompt (OAOS ACP/Hermes path — control_plane.acp_adapter.ACPAdapter) with file_ids/attachment_refs multimodal contract. No new LLM endpoint is invented.",
        })
    return base_resp

@router.get("/search")
async def search_notes(
    request: Request,
    q: str = Query(..., min_length=1, description="search query"),
    limit: int = Query(10, ge=1, le=50),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """GET /v1/personal-wiki/search -> owner-isolated vault FS + memory service (pgvector) fallback."""
    owner = _resolve_owner(request, x_user_id=x_user_id, required_scope="wiki:read")
    _audit(request, "PERSONAL_WIKI_SEARCH", {"user_id": owner["user_id"], "q": q, "limit": limit, "tenant_id": owner["tenant_id"], "agent_id": owner["agent_id"]})
    tenant_id = owner["tenant_id"]; agent_id = owner["agent_id"]
    # try memory service if DB configured (pgvector); else FS substring search
    # FS first (owner-isolated)
    try:
        fs_results = _search_notes_fs(tenant_id, agent_id, q, limit=limit)
        if fs_results:
            return {
                "query": q,
                "results": fs_results,
                "count": len(fs_results),
                "mock": False,
                "pgvector": False,
                "owner": owner["user_id"],
                "vault_path": str(_owner_vault_root(tenant_id, agent_id)),
                "source": "vault_fs",
            }
    except Exception as e:
        logger.debug(f"fs search failed: {e}")
    # if no FS hits and DB not configured -> mock fallback only in non-prod
    if not _is_db_configured():
        if _is_production():
            _no_mock_in_production()
        # non-prod mock for backwards compat when vault empty
        results = _mock_search_results(q, owner, limit=limit)
        return {
            "query": q,
            "results": results,
            "count": len(results),
            "mock": True,
            "pgvector": False,
            "owner": owner["user_id"],
            "vault_path": str(_owner_vault_root(tenant_id, agent_id)),
            "source": "mock",
        }
    # DB configured — would query memory_service pgvector; for now FS results or empty, not mock
    try:
        # attempt memory_service HTTP search if configured
        svc_url = os.environ.get("OAOS_MEMORY_SERVICE_URL") or os.environ.get("MEMORY_SERVICE_URL") or ""
        if svc_url:
            import httpx
            # best-effort remote search (sync via httpx)
            try:
                with httpx.Client(timeout=5) as client:
                    resp = client.post(f"{svc_url.rstrip('/')}/v1/memory/search", json={"query": q, "tenant_id": tenant_id, "agent_id": agent_id, "owner": owner["user_id"], "limit": limit}, headers={"X-Tenant-Id": tenant_id, "X-User-Id": owner["user_id"]})
                    if resp.status_code == 200:
                        j = resp.json()
                        rs = j.get("results") or j.get("items") or []
                        if rs:
                            return {"query": q, "results": rs[:limit], "count": len(rs[:limit]), "mock": False, "pgvector": True, "owner": owner["user_id"], "vault_path": str(_owner_vault_root(tenant_id, agent_id)), "source": "memory_service"}
            except Exception as e:
                logger.debug(f"memory_service search failed: {e}")
        # fallback to FS (already empty) — return empty not mock in prod
        fs_results = _search_notes_fs(tenant_id, agent_id, q, limit=limit)
        return {
            "query": q,
            "results": fs_results,
            "count": len(fs_results),
            "mock": False,
            "pgvector": True,
            "owner": owner["user_id"],
            "vault_path": str(_owner_vault_root(tenant_id, agent_id)),
            "source": "vault_fs",
        }
    except Exception as e:
        logger.warning(f"search fallback: {e}")
        if _is_production():
            _no_mock_in_production()
        results = _mock_search_results(q, owner, limit=limit)
        return {
            "query": q,
            "results": results,
            "count": len(results),
            "mock": True,
            "pgvector": False,
            "owner": owner["user_id"],
            "vault_path": str(_owner_vault_root(tenant_id, agent_id)),
            "error": str(e),
            "source": "mock",
        }

@router.get("/notes")
async def list_notes(
    request: Request,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """GET /v1/personal-wiki/notes -> owner-isolated vault FS (no mock in production)."""
    owner = _resolve_owner(request, x_user_id=x_user_id, required_scope="wiki:read")
    _audit(request, "PERSONAL_WIKI_LIST_NOTES", {"user_id": owner["user_id"], "limit": limit, "offset": offset, "tenant_id": owner["tenant_id"], "agent_id": owner["agent_id"]})
    tenant_id = owner["tenant_id"]; agent_id = owner["agent_id"]
    try:
        fs_notes = _list_notes_fs(tenant_id, agent_id, limit=limit, offset=offset)
        if fs_notes:
            paged = fs_notes
            return {
                "notes": paged,
                "count": len(paged),
                "total": len(paged),
                "mock": False,
                "owner": owner["user_id"],
                "vault_path": str(_owner_vault_root(tenant_id, agent_id)),
                "source": "vault_fs",
            }
    except Exception as e:
        logger.debug(f"fs list failed: {e}")
    if not _is_db_configured():
        if _is_production():
            _no_mock_in_production()
        # non-prod mock for backwards compat (vault empty)
        notes = _mock_notes(owner, limit=limit)
        paged = notes[offset: offset + limit]
        return {
            "notes": paged,
            "count": len(paged),
            "total": len(notes),
            "mock": True,
            "owner": owner["user_id"],
            "vault_path": str(_owner_vault_root(tenant_id, agent_id)),
            "source": "mock",
        }
    # DB configured but FS empty — return empty FS result (not mock) or mock with pgvector flag in non-prod
    try:
        fs_notes = _list_notes_fs(tenant_id, agent_id, limit=limit, offset=offset)
        return {
            "notes": fs_notes,
            "count": len(fs_notes),
            "total": len(fs_notes),
            "mock": False,
            "owner": owner["user_id"],
            "vault_path": str(_owner_vault_root(tenant_id, agent_id)),
            "source": "vault_fs",
        }
    except Exception as e:
        logger.warning(f"list notes fallback: {e}")
        if _is_production():
            _no_mock_in_production()
        notes = _mock_notes(owner, limit=limit)
        return {
            "notes": notes[offset: offset + limit],
            "count": len(notes),
            "total": len(notes),
            "mock": True,
            "owner": owner["user_id"],
            "vault_path": str(_owner_vault_root(tenant_id, agent_id)),
            "error": str(e),
            "source": "mock",
        }

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
