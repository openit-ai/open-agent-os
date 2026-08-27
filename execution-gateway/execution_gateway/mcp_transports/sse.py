"""SSE MCP Transport — httpx SSE client (Server-Sent Events)

MCP SSE spec (2024-11-05):
  Client opens GET <url>/sse  (EventSource) → receives `endpoint` event with POST URL
  Then client POSTs JSON-RPC messages to the endpoint URL
  Server pushes responses as SSE data events

This implementation handles both:
  1) Spec-compliant SSE (endpoint negotiation)
  2) Simple SSE streaming fallback (direct POST to url + GET SSE for notifications)

If the server does not emit `endpoint` event, POST goes directly to self.url.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import httpx

from .base import MCPBaseTransport, MCPTransportError, make_request, make_notification


def _parse_sse_events(text: str) -> list[dict]:
    """Parse raw SSE text into list of {event, data} dicts."""
    events: list[dict] = []
    cur_event = "message"
    cur_data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("event:"):
            cur_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            cur_data_lines.append(line[len("data:"):].lstrip())
        elif line == "":
            if cur_data_lines:
                data_str = "\n".join(cur_data_lines)
                events.append({"event": cur_event, "data": data_str})
                cur_event = "message"
                cur_data_lines = []
    if cur_data_lines:
        events.append({"event": cur_event, "data": "\n".join(cur_data_lines)})
    return events


class SSETransport(MCPBaseTransport):
    """MCP transport over Server-Sent Events."""

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        sse_timeout: float = 60.0,
        auth_token: str | None = None,
    ):
        super().__init__(timeout=timeout)
        self.url = url.rstrip("/")
        self.headers = headers or {}
        if auth_token:
            self.headers["Authorization"] = f"Bearer {auth_token}"
        self.sse_timeout = sse_timeout
        self._client: httpx.AsyncClient | None = None
        self._endpoint_url: str | None = None
        self._sse_task: asyncio.Task | None = None
        self._pending: dict[Any, asyncio.Future[dict]] = {}
        self._session_id: str | None = None

    async def connect(self) -> None:
        if self._connected:
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout + 5),
            headers={"Accept": "text/event-stream", **self.headers},
        )
        # Open SSE stream to discover endpoint
        # Non-blocking: start background task that keeps SSE open
        self._connected = True
        self._sse_task = asyncio.create_task(self._sse_loop())
        # Give SSE loop a moment to discover endpoint (but don't fail if it doesn't)
        await asyncio.sleep(0.3)
        # If endpoint not discovered within short time, fallback to direct url
        if not self._endpoint_url:
            self._endpoint_url = self.url

    async def disconnect(self) -> None:
        self._connected = False
        self._initialized = False
        if self._sse_task and not self._sse_task.done():
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPTransportError("SSE transport disconnected"))
        self._pending.clear()
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def _sse_loop(self) -> None:
        """Maintain SSE GET connection, route incoming messages to pending futures."""
        if not self._client:
            return
        url = self.url
        # Try /sse suffix if base url doesn't already end with /sse
        sse_url = url if url.endswith("/sse") else f"{url}/sse"
        candidates = [sse_url, url]
        for attempt_url in candidates:
            if not self._connected:
                return
            try:
                async with self._client.stream("GET", attempt_url, headers={"Accept": "text/event-stream", **self.headers}, timeout=self.sse_timeout) as resp:
                    if resp.status_code not in (200, 201):
                        continue
                    # Check for session id header
                    sid = resp.headers.get("mcp-session-id") or resp.headers.get("x-mcp-session-id")
                    if sid:
                        self._session_id = sid
                    async for line in resp.aiter_lines():
                        if not self._connected:
                            return
                        if not line or line.startswith(":"):
                            continue
                        # Accumulate SSE event — we handle line-by-line simple case
                        # Full SSE events are multi-line; we try to parse data lines directly
                        if line.startswith("event:"):
                            evt = line[len("event:"):].strip()
                            if evt == "endpoint":
                                # Next data line will contain endpoint URL
                                continue
                            continue
                        if line.startswith("data:"):
                            data_str = line[len("data:"):].lstrip()
                            # Check if this is endpoint negotiation
                            if data_str.startswith("/") or data_str.startswith("http"):
                                # Likely endpoint URL
                                if not self._endpoint_url or self._endpoint_url == self.url:
                                    # Resolve relative endpoint
                                    if data_str.startswith("http"):
                                        self._endpoint_url = data_str
                                    else:
                                        # Relative to base url
                                        base = self.url.rsplit("/sse", 1)[0].rstrip("/")
                                        self._endpoint_url = f"{base}{data_str}" if data_str.startswith("/") else f"{base}/{data_str}"
                                    continue
                            # Try to parse as JSON-RPC response
                            try:
                                msg = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            msg_id = msg.get("id")
                            if msg_id is not None and msg_id in self._pending:
                                fut = self._pending.pop(msg_id)
                                if not fut.done():
                                    if "error" in msg and msg["error"] is not None:
                                        err = msg["error"]
                                        fut.set_exception(MCPTransportError(f"RPC error {err.get('code')}: {err.get('message')}"))
                                    else:
                                        fut.set_result(msg)
                            # Also handle result without id pending (sometimes server echoes)
                    break  # clean exit
            except asyncio.CancelledError:
                return
            except Exception:
                if not self._connected:
                    return
                await asyncio.sleep(1.0)
                continue

    async def _post(self, payload: dict) -> dict | None:
        """POST JSON-RPC to endpoint. Returns parsed response if available inline, else None (await SSE)."""
        if not self._client:
            raise MCPTransportError("SSE client not connected")
        target = self._endpoint_url or self.url
        hdrs: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **self.headers}
        if self._session_id:
            hdrs["mcp-session-id"] = self._session_id
        try:
            resp = await self._client.post(target, json=payload, headers=hdrs)
        except httpx.RequestError as e:
            raise MCPTransportError(f"SSE POST failed to {target}: {e}") from e

        if resp.status_code not in (200, 201, 202):
            body = resp.text[:500] if resp.text else ""
            raise MCPTransportError(f"SSE POST {target} returned {resp.status_code}: {body}")

        # Capture session id if returned
        sid = resp.headers.get("mcp-session-id") or resp.headers.get("x-mcp-session-id")
        if sid:
            self._session_id = sid

        # If response is JSON, return directly
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype:
            try:
                data = resp.json()
                if isinstance(data, dict) and "jsonrpc" in data:
                    return data
            except Exception:
                pass
        # If SSE stream, parse events
        if "text/event-stream" in ctype:
            events = _parse_sse_events(resp.text)
            for evt in events:
                try:
                    data = json.loads(evt["data"])
                    if isinstance(data, dict) and "jsonrpc" in data:
                        return data
                except json.JSONDecodeError:
                    continue
            return None
        # Empty or 202 accepted — response will come via SSE stream
        if resp.text and resp.text.strip():
            try:
                data = json.loads(resp.text)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
        return None

    async def send_request(self, method: str, params: dict | None = None) -> dict:
        if not self._connected:
            raise MCPTransportError("not connected — call connect() first")
        msg_id = self._next_id()
        req = make_request(method, params, msg_id)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[msg_id] = fut

        # Try POST — if it returns inline response, use it
        inline = await self._post(req)
        if inline is not None:
            # Got inline response — resolve pending and return
            self._pending.pop(msg_id, None)
            if "error" in inline and inline["error"] is not None:
                err = inline["error"]
                raise MCPTransportError(f"RPC error {err.get('code')}: {err.get('message')}")
            result = inline.get("result", inline)
            # If result is wrapped with jsonrpc envelope, unwrap
            if isinstance(inline, dict) and "result" in inline:
                return inline["result"]
            return result if isinstance(result, dict) else {"result": result}

        # Otherwise wait for SSE response
        try:
            raw = await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(msg_id, None)
            raise MCPTransportError(f"SSE request timeout for {method} (id={msg_id})") from e

        if "error" in raw and raw["error"] is not None:
            err = raw["error"]
            raise MCPTransportError(f"RPC error {err.get('code')}: {err.get('message')}")
        return raw.get("result", raw)

    async def send_notification(self, method: str, params: dict | None = None) -> None:
        if not self._connected or not self._client:
            return
        notif = make_notification(method, params)
        try:
            await self._post(notif)
        except Exception:
            pass
