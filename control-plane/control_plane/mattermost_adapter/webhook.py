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

Extended for Phase 1 MVP (Section 3.1):
  If text contains "정리해줘" keyword, route to morning-briefing orchestrator
  and return briefing JSON directly (demo parity with POST /v1/demo/morning-briefing).
"""
from __future__ import annotations
import hmac
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Header, HTTPException, Request

from ..identity import map_user_to_agent
from ..session import session_store, new_request_id
from ..router import route_session
from ..acp_adapter import ACPAdapter
from ..config import settings

router = APIRouter()

# Lazy import for orchestrator (avoid circular at import time)
def _load_orchestrator():
    ROOT = Path(__file__).resolve().parents[3]
    for p in [ROOT / "examples" / "morning-briefing", ROOT / "execution-gateway", ROOT / "security" / "policy-engine", ROOT / "packages" / "policy-model", ROOT / "packages" / "audit-model", ROOT / "packages" / "common-types"]:
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    try:
        from orchestrator import run_morning_briefing  # type: ignore
        return run_morning_briefing
    except Exception:
        try:
            from morning_briefing.orchestrator import run_morning_briefing  # type: ignore
            return run_morning_briefing
        except Exception:
            return None

BRIEFING_KEYWORDS = ["정리해줘", "브리핑", "업무 정리", "오늘 업무"]


def _is_briefing_request(text: str) -> bool:
    return any(kw in text for kw in BRIEFING_KEYWORDS)

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

    # ── Phase 1 MVP: "정리해줘" keyword → demo orchestrator routing ──
    if _is_briefing_request(text):
        run_briefing = _load_orchestrator()
        if run_briefing is not None:
            agent_ctx = {
                "tenant_id": tenant_id,
                "user_id": mapping.human_principal,
                "agent_id": mapping.agent_principal,
                "session_id": session_id,
                "trace_id": rec.trace_id,
                "request_id": new_request_id(),
                "security_domain": mapping.security_domain,
            }
            briefing_result = await run_briefing(agent_ctx, tenant_id)
            # Also store prompt/stream for audit continuity
            rid = new_request_id()
            session_store.append_prompt(session_id, user_id, text, rid)
            session_store.append_stream_event(session_id, {"type": "briefing", "data": briefing_result, "trace_id": rec.trace_id})
            return {
                "received": True,
                "routed": "morning-briefing",
                "session_id": session_id,
                "agent_id": mapping.agent_principal,
                "trace_id": rec.trace_id,
                "request_id": rid,
                "briefing": briefing_result.get("briefing"),
                "sources": briefing_result.get("sources"),
                "approvals_required": briefing_result.get("approvals_required"),
                "audit": briefing_result.get("audit"),
                # Keep legacy acp field for compatibility
                "acp": {"status": "routed_to_briefing"},
            }

    # Forward prompt (non-briefing path)
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
