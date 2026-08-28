"""ACP Adapter — Internal Agent Interface ↔ Hermes ACP (Section 17).

Design: Internal Agent Interface is the canonical contract.
ACP is an adapter — not the canonical protocol. Hermes core is NOT modified.

Wire: Hermes ACP (Client ↔ Agent) via stdio/SSE/WebSocket depending on deployment.
This adapter translates:
  create_session / send_prompt / stream_event  ↔  Hermes ACP messages

Fallback v1.5.1: When ACP endpoint is unavailable (404), fall back to
Hermes Gateway OpenAI-compatible /v1/chat/completions (same LLM as
hermes @openit CoCo) — not Ollama. Uses OAOS_CP_HERMES_API_KEY.
"""
from __future__ import annotations
import asyncio
import json
import uuid
import os
from pathlib import Path
from typing import AsyncGenerator, Any
import httpx
from .session import SessionRecord

def _resolve_workspace_for_session(session: SessionRecord) -> str | None:
    """Lazy workspace path — /home/hermes/workspaces/{tenant}/{agent}/{session}"""
    try:
        from runtime_adapter.workspace import WorkspaceResolver  # type: ignore
        return str(WorkspaceResolver().resolve(session.tenant_id, session.agent_id, session.session_id))
    except Exception:
        try:
            import re as _re
            safe = lambda v: _re.sub(r"[^a-zA-Z0-9._-]", "_", str(v))[:64] or "default"
            return f"/home/hermes/workspaces/{safe(session.tenant_id)}/{safe(session.agent_id)}/{safe(session.session_id)}"
        except Exception:
            return None

class ACPAdapter:
    """Hermes ACP adapter — single integration point (Section 17)."""

    def __init__(self, hermes_base_url: str, timeout_s: float = 30.0):
        self.hermes_base_url = hermes_base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _headers(self, session: SessionRecord) -> dict[str, str]:
        # AgentContext propagated as headers (Section 18) + workspace
        headers = {
            "X-Tenant-Id": session.tenant_id,
            "X-User-Id": session.user_id,
            "X-Agent-Id": session.agent_id,
            "X-Session-Id": session.session_id,
            "X-Trace-Id": session.trace_id,
            "X-Security-Domain": session.security_domain,
        }
        # Lazy workspace header — per-session isolation (§16A.3.1)
        ws = getattr(session, "workspace", None)
        if not ws:
            ws = _resolve_workspace_for_session(session)
        if ws:
            headers["X-Workspace"] = ws
            headers["X-Workspace-Path"] = ws
        return headers

    def _hermes_api_key(self) -> str:
        try:
            from .config import settings
            k = getattr(settings, "hermes_api_key", "") or ""
            if k:
                return k
        except Exception:
            pass
        k = os.getenv("OAOS_CP_HERMES_API_KEY", "") or os.getenv("API_SERVER_KEY", "") or ""
        if k:
            return k
        # last resort: read from ~/.hermes/.env
        try:
            p = Path.home() / ".hermes" / ".env"
            for line in p.read_text().splitlines():
                if line.startswith("API_SERVER_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
        return ""

    def _hermes_model(self) -> str:
        try:
            from .config import settings
            m = getattr(settings, "hermes_model", "") or ""
            if m:
                return m
        except Exception:
            pass
        return os.getenv("OAOS_CP_HERMES_MODEL", "") or "qwen2.5"

    async def create_session_remote(self, session: SessionRecord, workspace: str | None = None) -> dict[str, Any]:
        """POST /acp/sessions — create Hermes-side session. Falls back to local if Hermes unavailable (dev)."""
        url = f"{self.hermes_base_url}/acp/sessions"
        ws = workspace or getattr(session, "workspace", None) or _resolve_workspace_for_session(session)
        payload = {
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "user_id": session.user_id,
            "tenant_id": session.tenant_id,
            "security_domain": session.security_domain,
            "trace_id": session.trace_id,
            "workspace": ws,
            "workspace_path": ws,
        }
        # remove None workspace if resolver failed (keep key but not None)
        if ws is None:
            payload.pop("workspace", None)
            payload.pop("workspace_path", None)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(url, json=payload, headers=self._headers(session))
                r.raise_for_status()
                return r.json()
        except Exception as e:
            # Dev fallback — Hermes not yet running
            return {"status": "local_fallback", "reason": str(e), "session_id": session.session_id, "workspace": ws}

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

        If Hermes ACP stream is unavailable (404), fall back to Hermes Gateway
        /v1/chat/completions (same LLM that powers @openit CoCo) and yield its
        reply as token stream. This keeps Mattermost @agent on the Hermes-configured LLM.
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
        # -- Hermes Gateway fallback (standard path — same LLM as @openit) --
        # Retrieve last user prompt from session_store
        prompt_text = ""
        try:
            from .session import session_store
            rec = session_store.get_any(session.session_id)
            if rec and rec.prompt_history:
                prompt_text = rec.prompt_history[-1].get("prompt", "") or ""
        except Exception:
            pass
        if prompt_text:
            api_key = self._hermes_api_key()
            model = self._hermes_model()
            gateway_url = f"{self.hermes_base_url}/v1/chat/completions"
            # Hermes gateway is at 8642, but config may point to wrong port — fixup if needed
            if ":8001" in gateway_url:
                gateway_url = gateway_url.replace(":8001", ":8642")
            system_prompt = (
                f"You are Open Agent OS personal agent {session.agent_id} for user {session.user_id} "
                f"(tenant {session.tenant_id}, session {session.session_id}). "
                "You are SEPARATE from Hermes @openit CoCo (company-wide). "
                "Reply in Korean, concise, helpful. Keep identity consistent."
            )
            try:
                async with httpx.AsyncClient(timeout=40.0) as client:
                    r = await client.post(
                        gateway_url,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": prompt_text},
                            ],
                            "temperature": 0.7,
                        },
                    )
                    r.raise_for_status()
                    data = r.json()
                    content = ""
                    try:
                        content = data["choices"][0]["message"]["content"] or ""
                    except Exception:
                        content = data.get("content", "") or ""
                    content = content.strip()
                    if content:
                        # yield in chunks to simulate streaming
                        mid = len(content) // 2
                        for chunk in ([content[:mid]] if mid and len(content) > 200 else [content]):
                            if chunk:
                                yield {"type": "token", "data": {"text": chunk}, "trace_id": session.trace_id}
                                await asyncio.sleep(0.02)
                        yield {"type": "done", "data": {}, "trace_id": session.trace_id}
                        return
            except Exception as e:
                # log and fall through — no synthetic, let agent runtime handle
                try:
                    import logging
                    logging.getLogger(__name__).warning(f"Hermes gateway fallback failed: {e}")
                except Exception:
                    pass
        # -- No synthetic fallback — strictly agent runtime only --
        # If Hermes gateway also unreachable, yield done without token so
        # Mattermost posts nothing (agent runtime will recover and retry).
        yield {"type": "done", "data": {}, "trace_id": session.trace_id}
