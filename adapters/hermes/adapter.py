"""Hermes adapter — Internal Agent Interface / ACP bridge (Section 17).

Wraps Hermes Runtime via ACP (Agent Control Plane) — session lifecycle,
prompt forwarding, stream events, trace propagation.

This is the MCP-facing adapter view of Hermes; the real transport is
ACPIAdapter / InternalAgentInterface. Skeleton uses httpx when
HERMES_BASE_URL is configured.

Env:
  HERMES_BASE_URL (e.g. http://hermes-runtime:8000)
  HERMES_API_KEY (optional bearer)
"""
from __future__ import annotations

import os
from typing import Any, AsyncIterator

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore


class HermesAdapter:
    """Hermes Runtime adapter — Internal Agent Interface (§17) + MCP tools."""

    name = "hermes"
    provider = "hermes"

    TOOL_ACTION: dict[str, str] = {
        "hermes_create_session": "CREATE",
        "hermes_send_prompt": "EXECUTE",
        "hermes_get_session": "READ",
        "hermes_stream_events": "READ",
        "hermes_cancel_session": "DELETE",
        "hermes_list_sessions": "SEARCH",
    }

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("HERMES_BASE_URL") or "http://localhost:8001").rstrip("/")
        self.api_key = api_key or os.getenv("HERMES_API_KEY") or ""
        # local session echo for skeleton mode
        self._sessions: dict[str, dict[str, Any]] = {}

    def _headers(self, agent_context: dict[str, Any] | Any | None = None) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if agent_context is not None:
            import json as _json
            ctx = agent_context if isinstance(agent_context, dict) else agent_context.model_dump() if hasattr(agent_context, "model_dump") else {}
            # propagate trace
            if isinstance(ctx, dict) and ctx.get("trace_id"):
                h["X-Trace-Id"] = str(ctx["trace_id"])
            try:
                h["X-Agent-Context"] = _json.dumps(ctx)
            except Exception:
                pass
        return h

    # ---- Session lifecycle (Internal Agent Interface §17) --------------------

    async def create_session(self, agent_context: dict[str, Any] | Any) -> dict[str, Any]:
        if not self.base_url or os.getenv("HERMES_MOCK", "") == "1":
            import uuid
            sid = f"sess_{uuid.uuid4().hex[:10]}"
            self._sessions[sid] = {"session_id": sid, "context": agent_context if isinstance(agent_context, dict) else {}}
            return {"_skeleton": True, "session_id": sid, "message": "HERMES_BASE_URL not set or mock — skeleton session"}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{self.base_url}/v1/sessions", json={"context": agent_context if isinstance(agent_context, dict) else {}}, headers=self._headers(agent_context))
            resp.raise_for_status()
            return resp.json()

    async def send_prompt(self, session_id: str, prompt: str, agent_context: dict[str, Any] | Any, request_id: str | None = None) -> dict[str, Any]:
        if not self.base_url or os.getenv("HERMES_MOCK", "") == "1":
            return {"_skeleton": True, "session_id": session_id, "prompt": prompt, "request_id": request_id}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        body: dict[str, Any] = {"prompt": prompt}
        if request_id:
            body["request_id"] = request_id
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{self.base_url}/v1/sessions/{session_id}/prompt", json=body, headers=self._headers(agent_context))
            resp.raise_for_status()
            return resp.json()

    async def get_session(self, session_id: str, agent_context: dict[str, Any] | Any | None = None) -> dict[str, Any]:
        if session_id in self._sessions:
            return self._sessions[session_id]
        if httpx is None or not self.base_url:
            return {"_skeleton": True, "session_id": session_id}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{self.base_url}/v1/sessions/{session_id}", headers=self._headers(agent_context))
            resp.raise_for_status()
            return resp.json()

    async def cancel_session(self, session_id: str, agent_context: dict[str, Any] | Any | None = None) -> dict[str, Any]:
        self._sessions.pop(session_id, None)
        if httpx is None or not self.base_url:
            return {"_skeleton": True, "session_id": session_id, "cancelled": True}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(f"{self.base_url}/v1/sessions/{session_id}", headers=self._headers(agent_context))
            # 404 is ok if already gone
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()
            return {"cancelled": True, "session_id": session_id}

    async def stream_events(self, session_id: str, agent_context: dict[str, Any] | Any | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yield stream events (SSE). Skeleton yields one mock event."""
        # Real: httpx streaming GET /v1/sessions/{id}/events
        yield {"type": "session_start", "session_id": session_id, "_skeleton": True}
        # In production: async for line in response.aiter_lines(): parse SSE

    # ---- MCP registry --------------------------------------------------------

    def tool_action(self, tool_name: str) -> str:
        return self.TOOL_ACTION.get(tool_name, "EXECUTE")

    async def list_tools(self) -> list[str]:
        return list(self.TOOL_ACTION.keys())

    async def list_resources(self) -> list[str]:
        return ["hermes/session/*", "hermes/agent/*"]

    def describe_tools(self) -> list[dict[str, Any]]:
        return [{"name": k, "action": v, "resource_pattern": "hermes/*"} for k, v in self.TOOL_ACTION.items()]

    async def call_tool(self, tool_name: str, args: dict[str, Any], agent_context: dict[str, Any] | Any) -> dict[str, Any]:
        if tool_name == "hermes_create_session":
            return await self.create_session(agent_context)
        if tool_name == "hermes_send_prompt":
            return await self.send_prompt(args.get("session_id", ""), args.get("prompt", ""), agent_context, request_id=args.get("request_id"))
        if tool_name == "hermes_get_session":
            return await self.get_session(args.get("session_id", ""), agent_context)
        if tool_name == "hermes_cancel_session":
            return await self.cancel_session(args.get("session_id", ""), agent_context)
        if tool_name == "hermes_list_sessions":
            return {"_skeleton": True, "sessions": list(self._sessions.keys())}
        if tool_name == "hermes_stream_events":
            events = []
            async for ev in self.stream_events(args.get("session_id", ""), agent_context):
                events.append(ev)
            return {"events": events}
        raise ValueError(f"unknown tool: {tool_name}")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "provider": self.provider, "tools": list(self.TOOL_ACTION.keys()), "resources": ["hermes/*"], "base_url": self.base_url, "has_api_key": bool(self.api_key)}
