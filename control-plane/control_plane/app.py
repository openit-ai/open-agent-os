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
from .internal_api import CreateSessionRequest, CreateSessionResponse, SendPromptRequest
from .mattermost_adapter.webhook import router as mattermost_router
from .demo import router as demo_router

app = FastAPI(title="Open Agent OS — Control Plane", version="0.1.1")
acp = ACPAdapter(settings.hermes_base_url)
app.include_router(mattermost_router, prefix="/v1", tags=["mattermost"])
app.include_router(demo_router, prefix="/v1", tags=["demo"])

def _caller_user(x_user_id: str | None) -> str:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="X-User-Id required (employee:...)")
    return x_user_id

@app.get("/health")
def health():
    return {"status": "ok", "tenant": settings.tenant_id, "workstream": "A"}

@app.post("/v1/sessions", response_model=CreateSessionResponse)
async def create_session(req: CreateSessionRequest, x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    caller = _caller_user(x_user_id or req.user_id)
    # Identity mapping — 1:1 logical agent
    mapping = map_user_to_agent(caller, req.tenant_id, req.security_domain)
    routing = route_session(req.security_domain)
    rec = session_store.create(
        tenant_id=req.tenant_id,
        user_id=mapping.human_principal,
        agent_id=mapping.agent_principal,
        security_domain=req.security_domain,
        hermes_worker=routing["pool"],
    )
    # Best-effort Hermes session creation (non-blocking for dev)
    await acp.create_session_remote(rec)
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
def get_session(session_id: str, x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    caller = _caller_user(x_user_id)
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
async def send_prompt(session_id: str, req: SendPromptRequest, x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    caller = _caller_user(x_user_id)
    rec = session_store.get(session_id, caller)
    rid = req.request_id or new_request_id()
    session_store.append_prompt(session_id, caller, req.prompt, rid)
    # Forward to Hermes via ACP
    result = await acp.send_prompt(rec, req.prompt, rid)
    # Also push a local stream event so SSE has something
    session_store.append_stream_event(session_id, {"type": "prompt_queued", "data": {"prompt": req.prompt, "request_id": rid}, "trace_id": rec.trace_id})
    return {"request_id": rid, "trace_id": rec.trace_id, "acp": result}

@app.get("/v1/sessions/{session_id}/stream")
async def stream(session_id: str, x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    caller = _caller_user(x_user_id)
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
def cancel_session(session_id: str, x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    caller = _caller_user(x_user_id)
    session_store.cancel(session_id, caller)
    return {"status": "cancelled", "session_id": session_id}

@app.get("/v1/context/{session_id}")
def get_agent_context(session_id: str, x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    caller = _caller_user(x_user_id)
    rec = session_store.get(session_id, caller)
    return rec.to_agent_context()
