"""Mattermost webhook → Control Plane → ACP → Hermes (Section 37).

Flow:
  Mattermost event (user message / mention)
    → verify signature (HMAC)
    → map Mattermost user → employee: principal
    → create or resume session (Workstream A)
    → forward prompt via ACPAdapter
    → stream response back to Mattermost (Bot post)

For Workstream A MVP: webhook accepts generic JSON (no real Mattermost server required).
Real verification uses MATTERMOST_WEBHOOK_SECRET.
"""
from __future__ import annotations
import hmac
import hashlib
import json
from typing import Any
from fastapi import APIRouter, Header, HTTPException, Request

from ..identity import map_user_to_agent
from ..session import session_store, new_request_id
from ..router import route_session
from ..acp_adapter import ACPAdapter
from ..config import settings

router = APIRouter()

def verify_mattermost_signature(body: bytes, signature: str | None, secret: str | None) -> bool:
    if not secret:
        return True  # dev: no secret configured → accept
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

@router.post("/mattermost/events")
async def mattermost_event(request: Request, x_signature: str | None = Header(default=None, alias="X-Mattermost-Signature")):
    body = await request.body()
    # In prod: settings.mattermost_webhook_secret
    secret = getattr(settings, "mattermost_webhook_secret", None)
    if not verify_mattermost_signature(body, x_signature, secret):
        raise HTTPException(status_code=401, detail="invalid mattermost signature")

    try:
        payload: dict[str, Any] = json.loads(body) if body else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON")

    # Expected payload (MVP): {"tenant_id": "...", "user_id": "employee:kim", "text": "...", "channel_id": "...", "session_id": "...?"}
    tenant_id: str = payload.get("tenant_id") or settings.tenant_id
    user_id: str = payload.get("user_id") or payload.get("user", {}).get("id", "")
    text: str = payload.get("text") or payload.get("message") or ""
    session_id: str | None = payload.get("session_id")

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id (employee:...) required")
    if not text:
        raise HTTPException(status_code=400, detail="text/message required")

    # Identity mapping — 1:1 logical agent
    mapping = map_user_to_agent(user_id, tenant_id)

    # Session: resume or create
    if session_id:
        try:
            rec = session_store.get(session_id, user_id)
        except (KeyError, PermissionError) as e:
            raise HTTPException(status_code=404 if isinstance(e, KeyError) else 403, detail=str(e))
    else:
        routing = route_session(mapping.security_domain)
        rec = session_store.create(
            tenant_id=tenant_id,
            user_id=mapping.human_principal,
            agent_id=mapping.agent_principal,
            security_domain=mapping.security_domain,
            hermes_worker=routing["pool"],
        )
        session_id = rec.session_id

    # Forward prompt
    rid = new_request_id()
    session_store.append_prompt(session_id, user_id, text, rid)
    acp = ACPAdapter(settings.hermes_base_url)
    acp_result = await acp.send_prompt(rec, text, rid)
    session_store.append_stream_event(session_id, {"type": "prompt_queued", "data": {"text": text, "request_id": rid}, "trace_id": rec.trace_id})

    return {
        "received": True,
        "session_id": session_id,
        "agent_id": mapping.agent_principal,
        "trace_id": rec.trace_id,
        "request_id": rid,
        "acp": acp_result,
    }

@router.get("/mattermost/health")
def mm_health():
    return {"status": "ok", "adapter": "mattermost", "workstream": "A"}
