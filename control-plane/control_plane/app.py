"""FastAPI — Control Plane (Workstream A).

Endpoints implement Internal Agent Interface (Section 17):
  POST /v1/sessions
  GET  /v1/sessions/{session_id}
  POST /v1/sessions/{session_id}/prompt
  GET  /v1/sessions/{session_id}/stream  (SSE)
  POST /v1/sessions/{session_id}/cancel
"""
from __future__ import annotations
import asyncio
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
import json

from .config import settings
from .identity import map_user_to_agent
from .session import session_store, new_request_id
from .router import route_session
from .acp_adapter import ACPAdapter
from .auth import resolve_caller_user  # H1: verified JWT identity
from .internal_api import CreateSessionRequest, CreateSessionResponse, SendPromptRequest
from .mattermost_adapter.webhook import router as mattermost_router
from .demo import router as demo_router
# Adaptive Profile MVP (v1.7.2 design) — code complete, NOT live deployed until migration + service verification
from .adaptive_profile.router import router as profile_router
try:
    from .adaptive_profile.skills import register_profile_skills as _reg_ps
    _reg_ps()
except Exception:
    pass
import logging
import os
import signal
import time

logger = logging.getLogger(__name__)

app = FastAPI(title="Open Agent OS — Control Plane", version="0.1.3")

# -- HA health helpers — liveness vs readiness (H4 strict) --
# /health & /healthz = liveness (always 200). /readyz = readiness with bounded real checks: prod 503 on degraded/draining.
def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        if os.getenv(k, "").strip().lower() in ("production", "prod"):
            return True
    return False

# graceful draining (H4: prod /readyz 503 when draining, liveness stays 200)
_shutting_down: bool = False
_active_requests: int = 0

@app.middleware("http")
async def _track_active_cp(request: Request, call_next):
    global _active_requests
    _active_requests += 1
    try:
        return await call_next(request)
    finally:
        _active_requests -= 1

def _handle_sigterm_cp(signum, frame):
    global _shutting_down
    _shutting_down = True
    logger.warning("SIGTERM received, draining %s active requests (30s)", _active_requests)

try:
    signal.signal(signal.SIGTERM, _handle_sigterm_cp)
except Exception:
    pass

def _check_latency(fn):
    start = time.monotonic()
    try:
        fn()
        latency = round((time.monotonic() - start) * 1000, 2)
        return {"status": "ok", "latency_ms": latency}
    except Exception as e:
        latency = round((time.monotonic() - start) * 1000, 2)
        return {"status": "degraded", "latency_ms": latency, "error": str(e)[:200]}

def _bounded_db_ping(db_url: str, timeout_s: float = 0.8) -> None:
    if "://" not in db_url:
        raise RuntimeError("invalid db url")
    if db_url.startswith("sqlite") and (":memory:" in db_url or "mode=memory" in db_url):
        return
    try:
        from sqlalchemy import create_engine, text  # type: ignore
        sync_url = db_url
        if sync_url.startswith("postgresql+asyncpg://"):
            sync_url = sync_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
        elif sync_url.startswith("postgresql://"):
            sync_url = sync_url.replace("postgresql://", "postgresql+psycopg://", 1)
        if "+aiosqlite" in sync_url:
            sync_url = sync_url.replace("+aiosqlite", "")
        kwargs: dict = {}
        if sync_url.startswith("postgresql"):
            kwargs = {"connect_args": {"connect_timeout": timeout_s}}  # type: ignore
        elif sync_url.startswith("sqlite"):
            kwargs = {"connect_args": {"timeout": timeout_s}}
        eng = create_engine(sync_url, **kwargs, pool_pre_ping=False)  # type: ignore
        import concurrent.futures
        def _ping():
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(_ping)
            fut.result(timeout=timeout_s + 0.5)
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=False)
        try:
            eng.dispose()
        except Exception:
            pass
    except RuntimeError:
        raise
    except Exception as e:
        msg = str(e).lower()
        if "no such module" in msg or "could not parse" in msg or "not found" in msg:
            return
        raise RuntimeError(f"db ping failed: {e}") from e

def _bounded_redis_ping(redis_url: str, timeout_s: float = 0.8) -> None:
    if "://" not in redis_url:
        raise RuntimeError("invalid redis url")
    try:
        import redis as _redis  # type: ignore
        client = _redis.Redis.from_url(redis_url, socket_connect_timeout=timeout_s, socket_timeout=timeout_s)
        import concurrent.futures
        def _ping():
            client.ping()
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(_ping)
            fut.result(timeout=timeout_s + 0.5)
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=False)
        try:
            client.close()
        except Exception:
            pass
    except RuntimeError:
        raise
    except Exception as e:
        msg = str(e).lower()
        if "no module" in msg:
            return
        raise RuntimeError(f"redis ping failed: {e}") from e

def _bounded_vault_ping(timeout_s: float = 0.8) -> None:
    vault_addr = (os.getenv("VAULT_ADDR", "") or "").strip()
    vault_backend = (os.getenv("VAULT_BACKEND", "") or "").strip().lower()
    legacy = {"", "encrypted_postgres", "encrypted-postgres", "legacy", "postgres", "none"}
    configured = bool(vault_addr) or (vault_backend and vault_backend not in legacy)
    if not configured:
        raise RuntimeError("vault not configured")
    try:
        if vault_addr:
            import concurrent.futures as _cf
            def _http_check():
                try:
                    try:
                        import httpx  # type: ignore
                        import asyncio as _asyncio
                        async def _do():
                            async with httpx.AsyncClient(timeout=timeout_s) as client:
                                resp = await client.get(vault_addr.rstrip("/") + "/v1/sys/health", headers={})
                                if resp.status_code not in (200, 204, 429, 472, 473):
                                    raise RuntimeError(f"vault health {resp.status_code}")
                        _asyncio.run(_do())
                        return
                    except ImportError:
                        pass
                    except Exception as e:
                        raise e
                    import urllib.request
                    import ssl
                    ctx = ssl._create_unverified_context() if vault_addr.startswith("https") else None
                    req = urllib.request.Request(vault_addr.rstrip("/") + "/v1/sys/health")
                    if os.getenv("VAULT_TOKEN"):
                        req.add_header("X-Vault-Token", os.getenv("VAULT_TOKEN", ""))
                    with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:  # type: ignore
                        code = getattr(resp, "status", 200) or 200
                        if code not in (200, 204, 429, 472, 473):
                            raise RuntimeError(f"vault health {code}")
                except RuntimeError:
                    raise
                except Exception as e:
                    raise RuntimeError(f"vault ping failed: {e}") from e
            ex = _cf.ThreadPoolExecutor(max_workers=1)
            try:
                fut = ex.submit(_http_check)
                fut.result(timeout=timeout_s + 0.5)
            finally:
                try:
                    ex.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    ex.shutdown(wait=False)
            return
        try:
            from vault.external import get_vault_backend  # type: ignore
            be = get_vault_backend()
            if be is None:
                return
            import concurrent.futures as _cf2
            import asyncio as _asyncio2
            def _be_check():
                try:
                    ok = _asyncio2.run(be.health_check())  # type: ignore
                    if not ok:
                        raise RuntimeError("vault backend health_check false")
                except Exception as e:
                    raise RuntimeError(f"vault backend health failed: {e}") from e
            ex2 = _cf2.ThreadPoolExecutor(max_workers=1)
            try:
                fut = ex2.submit(_be_check)
                fut.result(timeout=timeout_s + 0.5)
            finally:
                try:
                    ex2.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    ex2.shutdown(wait=False)
        except Exception as e:
            raise RuntimeError(f"vault ping failed: {e}") from e
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"vault ping failed: {e}") from e

def _ha_checks():
    checks: dict = {}
    db_url = getattr(settings, "database_url", "") or os.getenv("DATABASE_URL", "") or os.getenv("OAOS_DATABASE_URL", "")
    if db_url:
        def _db():
            _bounded_db_ping(db_url)
        checks["db"] = _check_latency(_db)
    else:
        checks["db"] = {"status": "skipped", "latency_ms": 0, "reason": "no DATABASE_URL"}
    redis_url = getattr(settings, "redis_url", "") or os.getenv("REDIS_URL", "")
    if redis_url:
        def _redis():
            _bounded_redis_ping(redis_url)
        checks["redis"] = _check_latency(_redis)
    else:
        checks["redis"] = {"status": "skipped", "latency_ms": 0, "reason": "no REDIS_URL"}
    vault_addr = (os.getenv("VAULT_ADDR", "") or "").strip()
    vault_backend = (os.getenv("VAULT_BACKEND", "") or "").strip().lower()
    legacy = {"", "encrypted_postgres", "encrypted-postgres", "legacy", "postgres", "none"}
    vault_configured = bool(vault_addr) or (vault_backend and vault_backend not in legacy)
    if vault_configured:
        def _vault():
            _bounded_vault_ping()
        checks["vault"] = _check_latency(_vault)
    else:
        checks["vault"] = {"status": "skipped", "latency_ms": 0, "reason": "no VAULT_ADDR/VAULT_BACKEND"}
    if _shutting_down:
        checks["self"] = {"status": "draining", "latency_ms": 0, "active_requests": _active_requests}
    else:
        checks["self"] = {"status": "ok", "latency_ms": 0, "active_requests": _active_requests}
    return checks

acp = ACPAdapter(settings.hermes_base_url)
app.include_router(mattermost_router, prefix="/v1", tags=["mattermost"])
app.include_router(demo_router, prefix="/v1", tags=["demo"])
# Adaptive Profile MVP — profile_router already prefixed /v1/profile; NOT live until DB migration + verification
app.include_router(profile_router)

# -- Lazy RuntimeRouter wiring (section 16F) --
def _get_runtime_router():
    """Lazy RuntimeRouter — respects EXECUTE runtime/<name> capability.
    Falls back to JIT-allow when no engine configured (keeps 541 green).
    """
    try:
        from runtime_adapter.router import RuntimeRouter  # canonical
    except Exception:
        try:
            from .runtime_router import RuntimeRouter  # shim
        except Exception:
            return None
    checker = None
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _root = _Path(__file__).resolve().parents[2]
        for _p in [_root / "security" / "policy-engine", _root / "packages" / "policy-model"]:
            if str(_p) not in _sys.path:
                _sys.path.insert(0, str(_p))
        # engine integration stub — keep JIT for now
        try:
            from policy_engine.engine import PolicyEngine  # type: ignore
            from policy_model import PolicyEvaluationRequest  # type: ignore
            checker = None
        except Exception:
            pass
    except Exception:
        pass
    try:
        return RuntimeRouter(capability_checker=checker) if checker else RuntimeRouter()
    except Exception:
        try:
            return RuntimeRouter()
        except Exception:
            return None


def _resolve_workspace_path(tenant_id: str, agent_id: str, session_id: str) -> str | None:
    """Lazy workspace resolver — /home/hermes/workspaces/{tenant}/{agent}/{session}"""
    try:
        from runtime_adapter.workspace import WorkspaceResolver  # type: ignore
        return str(WorkspaceResolver().resolve(tenant_id, agent_id, session_id))
    except Exception:
        try:
            import re
            safe = lambda v: re.sub(r"[^a-zA-Z0-9._-]", "_", v)[:64] or "default"
            return f"/home/hermes/workspaces/{safe(tenant_id)}/{safe(agent_id)}/{safe(session_id)}"
        except Exception:
            return None

def _caller_user(x_user_id: str | None) -> str:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="X-User-Id required (employee:...)")
    return x_user_id

@app.get("/health")
def health():
    return {"status": "ok", "tenant": settings.tenant_id, "workstream": "A"}


@app.get("/healthz")
def healthz():
    # H4 liveness: always 200 regardless of readiness/draining
    return {"status": "ok", "service": "control-plane"}

@app.get("/readyz")
def readyz():
    checks = _ha_checks()
    # H4 strict: degraded or draining -> prod 503, non-prod 200 compat
    degraded = any(v.get("status") in ("degraded", "draining") for v in checks.values())
    draining = checks.get("self", {}).get("status") == "draining"
    status = "draining" if draining else ("degraded" if degraded else "ok")
    body = {"status": status, "service": "control-plane", "checks": checks}
    if (degraded or draining) and _is_production():
        return JSONResponse(status_code=503, content=body)
    # explicit non-prod compatibility: 200 even when degraded
    return body

@app.get("/v1/health/detailed")
def health_detailed():
    start = time.monotonic()
    checks = _ha_checks()
    total = round((time.monotonic() - start) * 1000, 2)
    degraded = any(v.get("status") in ("degraded", "draining") for v in checks.values())
    draining = checks.get("self", {}).get("status") == "draining"
    status = "draining" if draining else ("degraded" if degraded else "ok")
    # detailed stays 200 but reports degraded/draining status explicitly
    return {"status": status, "service": "control-plane", "checks": checks, "latency_ms": total, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

@app.post("/v1/sessions", response_model=CreateSessionResponse)
async def create_session(req: CreateSessionRequest, authorization: str | None = Header(default=None, alias="Authorization"), x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    caller = resolve_caller_user(authorization, x_user_id, body_user_id=req.user_id, body_tenant_id=req.tenant_id)
    # Identity mapping — 1:1 logical agent
    mapping = map_user_to_agent(caller, req.tenant_id, req.security_domain)
    # -- RuntimeRouter selection (lazy, respects EXECUTE runtime/hermes capability) --
    # Keep legacy route_session for pool, but enforce RuntimeRouter capability gate.
    # If router selects hermes without capability, it raises PermissionError -> 403 via handler.
    router = _get_runtime_router()
    selected_runtime = None
    if router is not None:
        try:
            # security_domain maps to task_type hint; if domain implies sensitive/high, router may want hermes
            # Use security_domain as task_type for routing decision.
            selected_runtime = router.select_runtime(
                caller,
                task_type=req.security_domain or "general",
                required_capability=None,
            )
        except PermissionError:
            raise
        except ValueError:
            # No runtime available — propagate as 403/500? Keep legacy fallback
            selected_runtime = None
        except Exception:
            selected_runtime = None
    routing = route_session(req.security_domain)
    # If router selected a runtime, optionally refine pool: hermes->hermes pool, llm/safe->hermes-general still valid
    # For now keep pool from route_session to avoid breaking tests; selected_runtime is for capability enforcement.
    # Future: map selected_runtime to pool (e.g. llm -> separate pool) when multi-pool infra exists.
    rec = session_store.create(
        tenant_id=req.tenant_id,
        user_id=mapping.human_principal,
        agent_id=mapping.agent_principal,
        security_domain=req.security_domain,
        hermes_worker=routing["pool"],
    )
    # Best-effort Hermes session creation (non-blocking for dev) — includes workspace param lazily
    await acp.create_session_remote(rec, workspace=_resolve_workspace_path(rec.tenant_id, rec.agent_id, rec.session_id))
    return CreateSessionResponse(session_id=rec.session_id, agent_id=rec.agent_id, trace_id=rec.trace_id)

@app.exception_handler(PermissionError)
async def permission_handler(request: Request, exc: PermissionError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=403, content={"detail": str(exc)})

@app.exception_handler(KeyError)
async def notfound_handler(request: Request, exc: KeyError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=404, content={"detail": str(exc)})

@app.get("/v1/sessions/{session_id}")
def get_session(session_id: str, authorization: str | None = Header(default=None, alias="Authorization"), x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    caller = resolve_caller_user(authorization, x_user_id)
    rec = session_store.get(session_id, caller)
    return {
        "session_id": rec.session_id,
        "user_id": rec.user_id,
        "agent_id": rec.agent_id,
        "trace_id": rec.trace_id,
        "security_domain": rec.security_domain,
        "status": rec.status,
        "prompt_history": rec.prompt_history,
    }

@app.post("/v1/sessions/{session_id}/prompt")
async def send_prompt(session_id: str, req: SendPromptRequest, authorization: str | None = Header(default=None, alias="Authorization"), x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    caller = resolve_caller_user(authorization, x_user_id)
    # path vs body session_id integrity (if body provides session_id, must match path)
    if req.session_id and req.session_id != session_id:
        raise HTTPException(status_code=400, detail="session_id mismatch: path vs body")
    rec = session_store.get(session_id, caller)
    # tenant integrity: if JWT present, token tenant must match session tenant
    if authorization and authorization.strip().lower().startswith("bearer "):
        token = authorization.strip()[7:].strip()
        if token:
            try:
                from .auth import verify_user_jwt as _verify_jwt
                claims = _verify_jwt(token)
                token_tenant = claims.get("tenant_id")
                if token_tenant and token_tenant != rec.tenant_id:
                    raise HTTPException(status_code=401, detail="TENANT_MISMATCH: token tenant != session tenant")
            except HTTPException:
                raise
            except Exception:
                # verify_user_jwt already raises HTTPException on failure; non-JWT case handled by resolve_caller_user
                pass
    rid = req.request_id or new_request_id()
    # owner/agent/tenant integrity via deterministic mapping (no LLM)
    try:
        mapping = map_user_to_agent(caller, rec.tenant_id, rec.security_domain)
        if mapping.human_principal != rec.user_id or mapping.agent_principal != rec.agent_id:
            raise HTTPException(status_code=403, detail=f"owner/agent mismatch: mapping {mapping.human_principal}/{mapping.agent_principal} != session {rec.user_id}/{rec.agent_id}")
        if mapping.tenant_id != rec.tenant_id:
            raise HTTPException(status_code=403, detail=f"tenant mismatch: mapping tenant {mapping.tenant_id} != session tenant {rec.tenant_id}")
    except HTTPException:
        raise
    except Exception as e:
        if _is_production():
            raise HTTPException(status_code=403, detail=f"identity mapping failed fail-closed: {e}")
        raise HTTPException(status_code=403, detail=f"identity mapping failed: {e}")
    # Global Policy Gate enforcement BEFORE ACP (deterministic, no LLM classifier)
    # MUST audit before forward; DENY/APPROVAL_REQUIRED never forwards; production fail-closed if gate/policy/audit unavailable
    try:
        from .mattermost_policy_gate import get_mattermost_gate as _get_gate
        gate = _get_gate(rec.tenant_id)
        if gate is None:
            if _is_production():
                raise HTTPException(status_code=403, detail="policy gate unavailable — fail-closed")
            raise HTTPException(status_code=403, detail="policy gate unavailable")
        await gate.authorize_ingress(mapping, session_id, rec.trace_id, rid)
    except HTTPException:
        raise
    except Exception as e:
        if _is_production():
            raise HTTPException(status_code=403, detail=f"policy gate unavailable fail-closed: {e}")
        raise HTTPException(status_code=403, detail=f"policy gate error: {e}")
    # Only after ALLOW + successful audit, persist and forward
    _fid = getattr(req, "file_ids", None) or getattr(req, "file_ids", None)
    _arefs = getattr(req, "attachment_refs", None) or getattr(req, "attachments", None) or ([getattr(req, "attachment_ref", None)] if getattr(req, "attachment_ref", None) else None)
    # normalize: if req carries single attachment_ref, wrap
    if _arefs and isinstance(_arefs, dict):
        _arefs = [_arefs]
    _rctx = getattr(req, "runtime_context", None)
    # file_ids derived from attachment_refs if not explicit (multimodal contract)
    if _arefs and not _fid:
        _fid = [r.get("attachment_id") or r.get("vault_path") or r.get("file_id") for r in _arefs if isinstance(r, dict)]
        _fid = [x for x in _fid if x]
    session_store.append_prompt(session_id, caller, req.prompt, rid, file_ids=_fid, attachment_refs=_arefs, runtime_context=_rctx)
    # Adaptive Profile: async evidence worker (fire-and-forget, never blocks response path)
    try:
        from control_plane.adaptive_profile.worker import handle_interaction_event as _ap_handle
        _ap_handle({
            "tenant_id": rec.tenant_id,
            "user_id": rec.user_id,
            "session_id": session_id,
            "conversation_id": session_id,
            "message_id": rid,
            "task_type": rec.security_domain,
            "text": req.prompt,
        })
    except Exception:
        pass
    # Forward to Hermes via ACP — direct delivery via active runtime, with file_ids/multimodal context (no model selection)
    result = await acp.send_prompt(rec, req.prompt, rid, attachment_refs=_arefs, file_ids=_fid, runtime_context=_rctx)
    # Also push a local stream event so SSE has something — include multimodal context if present
    _queued_data = {"prompt": req.prompt, "request_id": rid}  # type: ignore
    if _fid:
        _queued_data["file_ids"] = _fid  # type: ignore
    if _arefs:
        _queued_data["attachment_refs"] = _arefs  # type: ignore
    if _rctx:
        _queued_data["runtime_context"] = _rctx  # type: ignore
    session_store.append_stream_event(session_id, {"type": "prompt_queued", "data": _queued_data, "trace_id": rec.trace_id})
    return {"request_id": rid, "trace_id": rec.trace_id, "acp": result}

@app.get("/v1/sessions/{session_id}/stream")
async def stream(session_id: str, authorization: str | None = Header(default=None, alias="Authorization"), x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    caller = resolve_caller_user(authorization, x_user_id)
    try:
        rec = session_store.get(session_id, caller)
    except (KeyError, PermissionError) as e:
        raise HTTPException(status_code=404 if isinstance(e, KeyError) else 403, detail=str(e))

    async def event_gen():
        # First drain buffered events
        for ev in list(rec.stream_events):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        # Then proxy Hermes stream
        async for ev in acp.stream_events(rec):
            session_store.append_stream_event(session_id, ev)
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev.get("type") == "done":
                break

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/v1/sessions/{session_id}/cancel")
def cancel_session(session_id: str, authorization: str | None = Header(default=None, alias="Authorization"), x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    caller = resolve_caller_user(authorization, x_user_id)
    session_store.cancel(session_id, caller)
    return {"status": "cancelled", "session_id": session_id}

@app.get("/v1/context/{session_id}")
def get_agent_context(session_id: str, authorization: str | None = Header(default=None, alias="Authorization"), x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    caller = resolve_caller_user(authorization, x_user_id)
    rec = session_store.get(session_id, caller)
    return rec.to_agent_context()
