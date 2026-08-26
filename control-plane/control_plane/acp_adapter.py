"""ACP Adapter — Internal Agent Interface ↔ Hermes ACP (Section 17).

Design: Internal Agent Interface is the canonical contract.
ACP is an adapter — not the canonical protocol. Hermes core is NOT modified.

Wire: Hermes ACP (Client ↔ Agent) via stdio/SSE/WebSocket depending on deployment.
This adapter translates:
  create_session / send_prompt / stream_event  ↔  Hermes ACP messages
"""
from __future__ import annotations
import asyncio
import json
import uuid
from typing import AsyncGenerator, Any
import httpx
from .session import SessionRecord

class ACPAdapter:
    """Hermes ACP adapter — single integration point (Section 17)."""

    def __init__(self, hermes_base_url: str, timeout_s: float = 30.0):
        self.hermes_base_url = hermes_base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _headers(self, session: SessionRecord) -> dict[str, str]:
        # AgentContext propagated as headers (Section 18)
        return {
            "X-Tenant-Id": session.tenant_id,
            "X-User-Id": session.user_id,
            "X-Agent-Id": session.agent_id,
            "X-Session-Id": session.session_id,
            "X-Trace-Id": session.trace_id,
            "X-Security-Domain": session.security_domain,
        }

    async def create_session_remote(self, session: SessionRecord) -> dict[str, Any]:
        """POST /acp/sessions — create Hermes-side session. Falls back to local if Hermes unavailable (dev)."""
        url = f"{self.hermes_base_url}/acp/sessions"
        payload = {
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "user_id": session.user_id,
            "tenant_id": session.tenant_id,
            "security_domain": session.security_domain,
            "trace_id": session.trace_id,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(url, json=payload, headers=self._headers(session))
                r.raise_for_status()
                return r.json()
        except Exception as e:
            # Dev fallback — Hermes not yet running
            return {"status": "local_fallback", "reason": str(e), "session_id": session.session_id}

    async def send_prompt(self, session: SessionRecord, prompt: str, request_id: str) -> dict[str, Any]:
        url = f"{self.hermes_base_url}/acp/sessions/{session.session_id}/prompt"
        payload = {"prompt": prompt, "request_id": request_id, "trace_id": session.trace_id}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(url, json=payload, headers=self._headers(session))
                r.raise_for_status()
                return r.json()
        except Exception as e:
            return {"status": "queued_local", "reason": str(e), "request_id": request_id}

    async def stream_events(self, session: SessionRecord) -> AsyncGenerator[dict[str, Any], None]:
        """SSE stream from Hermes — yields StreamEvent dicts (Section 17: stream_event).

        Dev fallback: yields synthetic events so Workstream A can be tested without Hermes.
        """
        url = f"{self.hermes_base_url}/acp/sessions/{session.session_id}/stream"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                async with client.stream("GET", url, headers=self._headers(session)) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(f"stream status {resp.status_code}")
                    async for line in resp.aiter_lines():
                        if not line or line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            data = line[5:].strip()
                            try:
                                yield json.loads(data)
                            except json.JSONDecodeError:
                                yield {"type": "token", "data": {"text": data}}
                    return
        except Exception:
            pass
        # ── Dev fallback synthetic stream (keeps Workstream A testable) ──
        for chunk in ["안녕하세요, ", "Personal Agent가 ", "준비되었습니다."]:
            await asyncio.sleep(0.02)
            yield {"type": "token", "data": {"text": chunk}, "trace_id": session.trace_id}
        yield {"type": "done", "data": {}, "trace_id": session.trace_id}
