"""Streamable HTTP MCP Transport — httpx POST + optional SSE response

MCP Streamable HTTP spec (2025-03-26 / 2024-11-05 streamable-http):
  Client POSTs JSON-RPC to <url> (single endpoint)
  Server may respond with:
    - application/json (single response)
    - text/event-stream (streamed progress + final result)
  Session management via `mcp-session-id` header.

This client handles both cases transparently.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .base import MCPBaseTransport, MCPTransportError, make_request, make_notification


def _parse_sse_response(text: str) -> list[dict]:
    """Extract JSON objects from SSE-formatted response text."""
    results: list[dict] = []
    cur_data: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            cur_data.append(line[len("data:"):].lstrip())
        elif line == "" and cur_data:
            blob = "\n".join(cur_data)
            cur_data.clear()
            if blob.strip() and blob.strip() != "[DONE]":
                try:
                    results.append(json.loads(blob))
                except json.JSONDecodeError:
                    pass
        elif line.startswith(":"):
            continue
    if cur_data:
        blob = "\n".join(cur_data)
        if blob.strip() and blob.strip() != "[DONE]":
            try:
                results.append(json.loads(blob))
            except json.JSONDecodeError:
                pass
    # Fallback: raw JSON
    if not results and text.strip().startswith("{"):
        try:
            results.append(json.loads(text))
        except json.JSONDecodeError:
            pass
    return results


class StreamableHTTPTransport(MCPBaseTransport):
    """MCP Streamable HTTP transport — POST JSON-RPC, handle JSON or SSE response."""

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        auth_token: str | None = None,
    ):
        super().__init__(timeout=timeout)
        self.url = url.rstrip("/")
        self.headers = headers or {}
        if auth_token:
            self.headers["Authorization"] = f"Bearer {auth_token}"
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None

    async def connect(self) -> None:
        if self._connected:
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout + 5),
            headers=self.headers,
        )
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._initialized = False
        if self._client:
            # Try to delete session if server uses it
            if self._session_id:
                try:
                    await self._client.delete(
                        self.url,
                        headers={"mcp-session-id": self._session_id, **self.headers},
                        timeout=5,
                    )
                except Exception:
                    pass
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
        self._session_id = None

    async def _do_post(self, payload: dict) -> dict:
        """POST payload and return parsed JSON-RPC envelope (or raise)."""
        if not self._client:
            raise MCPTransportError("streamable-http client not connected")
        hdrs: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self._session_id:
            hdrs["mcp-session-id"] = self._session_id

        try:
            resp = await self._client.post(self.url, json=payload, headers=hdrs)
        except httpx.RequestError as e:
            raise MCPTransportError(f"streamable-http POST failed to {self.url}: {e}") from e

        # Capture session id
        sid = resp.headers.get("mcp-session-id") or resp.headers.get("x-mcp-session-id")
        if sid:
            self._session_id = sid

        if resp.status_code in (404,):
            raise MCPTransportError(f"streamable-http {self.url} not found (404): {resp.text[:300]}")
        if resp.status_code not in (200, 201, 202):
            raise MCPTransportError(f"streamable-http POST {self.url} returned {resp.status_code}: {resp.text[:500]}")

        ctype = resp.headers.get("content-type", "")

        # Handle SSE response
        if "text/event-stream" in ctype:
            events = _parse_sse_response(resp.text)
            # Find response matching id if present, otherwise last
            want_id = payload.get("id")
            for evt in events:
                if isinstance(evt, dict) and evt.get("id") == want_id:
                    return evt
            if events:
                # Return last event (often the final result)
                return events[-1]
            raise MCPTransportError(f"streamable-http SSE response had no valid JSON-RPC messages: {resp.text[:500]}")

        # Handle JSON response
        if resp.text and resp.text.strip():
            try:
                data = resp.json()
                # Batch response (list) — find matching id
                if isinstance(data, list):
                    want_id = payload.get("id")
                    for item in data:
                        if isinstance(item, dict) and item.get("id") == want_id:
                            return item
                    return data[0] if data else {}
                return data
            except json.JSONDecodeError as e:
                raise MCPTransportError(f"invalid JSON response from {self.url}: {e}: {resp.text[:500]}") from e

        # 202 Accepted with no body — for notifications
        return {}

    async def send_request(self, method: str, params: dict | None = None) -> dict:
        if not self._connected:
            raise MCPTransportError("not connected — call connect() first")
        req = make_request(method, params, self._next_id())
        raw = await self._do_post(req)
        if isinstance(raw, dict) and "error" in raw and raw["error"] is not None:
            err = raw["error"]
            raise MCPTransportError(f"RPC error {err.get('code')}: {err.get('message')}")
        if isinstance(raw, dict) and "jsonrpc" in raw:
            return raw.get("result", raw)
        if isinstance(raw, dict) and "result" in raw:
            return raw["result"]
        return raw if isinstance(raw, dict) else {"result": raw}

    async def send_notification(self, method: str, params: dict | None = None) -> None:
        if not self._connected or not self._client:
            return
        notif = make_notification(method, params)
        try:
            await self._do_post(notif)
        except Exception:
            pass

    # ── Optional GET stream for server notifications ──────────────────

    async def open_sse_stream(self):
        """Open GET SSE stream to receive server-initiated notifications.

        Caller should iterate lines. This is optional and used for long-lived
        server push after initialization.
        """
        if not self._client:
            raise MCPTransportError("not connected")
        hdrs = {"Accept": "text/event-stream", **self.headers}
        if self._session_id:
            hdrs["mcp-session-id"] = self._session_id
        # Return async context manager for streaming
        return self._client.stream("GET", self.url, headers=hdrs)
