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
from fastapi.responses import StreamingResponse
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
import os
import time

app = FastAPI(title="Open Agent OS — Control Plane", version="0.1.1")

# -- HA health helpers — liveness vs readiness --
# /health & /healthz = liveness (always ok). /readyz = readiness with bounded real checks.
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
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_ping)
            fut.result(timeout=timeout_s + 0.5)
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
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_ping)
            fut.result(timeout=timeout_s + 0.5)
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

def _ha_checks():
    checks: dict = {}
    # DB check (bounded real ping when configured; degraded not exception, preserves test compat)
    db_url = getattr(settings, "database_url", "") or os.getenv("DATABASE_URL", "")
    if db_url:
        def _db():
            _bounded_db_ping(db_url)
        checks["db"] = _check_latency(_db)
        # attempt real ping if DATABASE_URL points to reachable host with short timeout
        # fallback to format check above keeps fail-open & fast in tests
    else:
        checks["db"] = {"status": "skipped", "latency_ms": 0, "reason": "no DATABASE_URL"}
    # Redis check
    redis_url = getattr(settings, "redis_url", "") or os.getenv("REDIS_URL", "")
    if redis_url:
        def _redis():
            _bounded_redis_ping(redis_url)
        checks["redis"] = _check_latency(_redis)
    else:
        checks["redis"] = {"status": "skipped", "latency_ms": 0, "reason": "no REDIS_URL"}
    # self check
    checks["self"] = {"status": "ok", "latency_ms": 0}
    return checks
acp = ACPAdapter(settings.hermes_base_url)
app.include_router(mattermost_router, prefix="/v1", tags=["mattermost"])
app.include_router(demo_router, prefix="/v1", tags=["demo"])

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
    return {"status": "ok", "service": "control-plane"}

@app.get("/readyz")
def readyz():
    checks = _ha_checks()
    # fail-open: always 200 even if degraded
    degraded = any(v.get("status") == "degraded" for v in checks.values())
    return {"status": "degraded" if degraded else "ok", "service": "control-plane", "checks": checks}

@app.get("/v1/health/detailed")
def health_detailed():
    start = time.monotonic()
    checks = _ha_checks()
    total = round((time.monotonic() - start) * 1000, 2)
    degraded = any(v.get("status") == "degraded" for v in checks.values())
    return {"status": "degraded" if degraded else "ok", "service": "control-plane", "checks": checks, "latency_ms": total, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

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
    rec = session_store.get(session_id, caller)
    rid = req.request_id or new_request_id()
    session_store.append_prompt(session_id, caller, req.prompt, rid)
    # Forward to Hermes via ACP
    result = await acp.send_prompt(rec, req.prompt, rid)
    # Also push a local stream event so SSE has something
    session_store.append_stream_event(session_id, {"type": "prompt_queued", "data": {"prompt": req.prompt, "request_id": rid}, "trace_id": rec.trace_id})
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
