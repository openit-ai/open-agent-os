"""MCP Base Transport — abstract interface + shared JSON-RPC helpers"""
from __future__ import annotations

import abc
import json
import uuid
from dataclasses import dataclass, field
from typing import Any


class MCPTransportError(RuntimeError):
    """Transport-level error (connection, timeout, protocol)."""


@dataclass
class MCPMessage:
    """JSON-RPC 2.0 message envelope."""
    jsonrpc: str = "2.0"
    id: Any = None
    method: str | None = None
    params: dict | None = None
    result: Any | None = None
    error: dict | None = None


# Protocol version negotiated during initialize
MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_CLIENT_INFO = {"name": "execution-gateway", "version": "0.1.3"}


def make_request(method: str, params: dict | None = None, msg_id: Any | None = None) -> dict:
    """Build JSON-RPC 2.0 request dict."""
    if msg_id is None:
        msg_id = uuid.uuid4().hex[:8]
    d: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        d["params"] = params
    return d


def make_notification(method: str, params: dict | None = None) -> dict:
    d: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        d["params"] = params
    return d


def parse_response(raw: str | bytes | dict) -> dict:
    """Parse raw JSON-RPC response, raise MCPTransportError on protocol error."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise MCPTransportError(f"invalid JSON-RPC response: {e}") from e
    else:
        data = raw
    if not isinstance(data, dict):
        raise MCPTransportError(f"expected JSON object, got {type(data)}")
    if data.get("jsonrpc") != "2.0":
        raise MCPTransportError(f"expected jsonrpc 2.0, got {data.get('jsonrpc')}")
    if "error" in data and data["error"] is not None:
        err = data["error"]
        raise MCPTransportError(f"JSON-RPC error {err.get('code')}: {err.get('message')} — {err}")
    return data


class MCPBaseTransport(abc.ABC):
    """Abstract MCP transport — STDIO / SSE / Streamable HTTP all conform."""

    def __init__(self, *, timeout: float = 30.0):
        self.timeout = timeout
        self._connected: bool = False
        self._initialized: bool = False
        self._request_id: int = 0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def initialized(self) -> bool:
        return self._initialized

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    # ── lifecycle ────────────────────────────────────────────────────

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish transport connection (spawn process, open SSE stream, etc.)."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Tear down connection."""

    @abc.abstractmethod
    async def send_request(self, method: str, params: dict | None = None) -> dict:
        """Send JSON-RPC request and await response. Returns result dict (the 'result' field)."""

    # ── MCP operations ───────────────────────────────────────────────

    async def initialize(self) -> dict:
        """MCP initialize handshake. Returns server capabilities."""
        result = await self.send_request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": MCP_CLIENT_INFO,
        })
        # Send initialized notification (no response expected)
        try:
            await self.send_notification("notifications/initialized", {})
        except Exception:
            pass
        self._initialized = True
        return result

    async def send_notification(self, method: str, params: dict | None = None) -> None:
        """Send JSON-RPC notification (no id, no response). Override if transport needs it."""
        # Default: send as request but ignore response — subclasses can override for true notifications
        pass

    async def list_tools(self) -> list[dict]:
        result = await self.send_request("tools/list", {})
        # MCP spec: result.tools is list
        if isinstance(result, dict) and "tools" in result:
            return result["tools"]
        if isinstance(result, list):
            return result
        return []

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        result = await self.send_request("tools/call", {"name": name, "arguments": arguments or {}})
        return result if isinstance(result, dict) else {"content": result}

    async def list_resources(self) -> list[dict]:
        result = await self.send_request("resources/list", {})
        if isinstance(result, dict) and "resources" in result:
            return result["resources"]
        if isinstance(result, list):
            return result
        return []

    async def ping(self) -> bool:
        try:
            await self.send_request("ping", {})
            return True
        except Exception:
            return False

    # ── context manager ──────────────────────────────────────────────

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.disconnect()
