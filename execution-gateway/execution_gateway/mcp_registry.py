"""MCP Registry — Section 7.2 고도화

- MCP Server 등록/해제
- tool / resource discovery (mock + 실연동)
- resource/action normalization 연동
- mcp_resource_model 연동
- Transport 실연동: stdio / sse / streamable-http (P1-2)
- heartbeat / status

한국어 주석: 설계 원칙 — MCP Registry는 discovery만 담당, 권한 판단은 PolicyEngine/authz_hook에서 수행
실연동은 httpx 기반 Transport로 수행 — mcp SDK 없이 JSON-RPC 2.0 직접 구현
연결 실패 시 mock 데이터로 fallback (기존 테스트 호환)
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    from .normalize import normalize_resource, canonicalize_action, parse_resource
except ImportError:
    from normalize import normalize_resource, canonicalize_action, parse_resource  # type: ignore

try:
    from mcp_resource_model.model import MCPTool, MCPResource  # type: ignore
except Exception:
    MCPTool = Any  # type: ignore
    MCPResource = Any  # type: ignore

logger = logging.getLogger(__name__)

# Allowed transports
ALLOWED_TRANSPORTS = frozenset({"stdio", "sse", "streamable-http", "mock"})


@dataclass
class MCPServer:
    name: str
    transport: str  # stdio / sse / streamable-http / mock
    command: str | None = None
    url: str | None = None
    args: list[str] | None = None
    headers: dict[str, str] | None = None
    timeout: float = 30.0
    tools: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    # 확장: 상세 MCPTool / MCPResource 객체
    tool_details: list[dict] = field(default_factory=list)
    resource_details: list[dict] = field(default_factory=list)
    # connector 바인딩 (google, outline 등)
    connector: str | None = None
    enabled: bool = True
    # 실연동 상태
    status: str = "disconnected"  # disconnected | connecting | connected | error
    last_heartbeat: datetime | None = None
    last_error: str | None = None
    # internal transport instance (not serialized)
    _transport: Any | None = field(default=None, repr=False, compare=False)

    def get_transport(self) -> Any | None:
        """Transport 인스턴스 생성 (lazy). None이면 mock 모드."""
        if self.transport == "mock":
            return None
        if self._transport is not None:
            return self._transport
        try:
            if self.transport == "stdio":
                from .mcp_transports.stdio import StdioTransport
                if not self.command:
                    logger.warning("stdio server %s: no command — fallback to mock", self.name)
                    return None
                self._transport = StdioTransport(
                    command=self.command,
                    args=self.args,
                    timeout=self.timeout,
                )
                return self._transport
            elif self.transport == "sse":
                from .mcp_transports.sse import SSETransport
                if not self.url:
                    logger.warning("sse server %s: no url — fallback to mock", self.name)
                    return None
                self._transport = SSETransport(url=self.url, headers=self.headers, timeout=self.timeout)
                return self._transport
            elif self.transport == "streamable-http":
                from .mcp_transports.streamable_http import StreamableHTTPTransport
                if not self.url:
                    logger.warning("streamable-http server %s: no url — fallback to mock", self.name)
                    return None
                self._transport = StreamableHTTPTransport(url=self.url, headers=self.headers, timeout=self.timeout)
                return self._transport
            else:
                logger.warning("unknown transport %s for server %s", self.transport, self.name)
                return None
        except ImportError as e:
            logger.warning("transport import failed for %s: %s", self.name, e)
            return None
        except Exception as e:
            logger.warning("transport creation failed for %s: %s", self.name, e)
            return None

    async def connect(self) -> bool:
        """Transport 연결 + initialize. 성공 시 True."""
        if self.transport == "mock":
            self.status = "connected"
            return True
        transport = self.get_transport()
        if transport is None:
            # mock fallback
            self.status = "connected"
            return True
        try:
            self.status = "connecting"
            await transport.connect()
            # MCP handshake
            try:
                await transport.initialize()
            except Exception as e:
                logger.warning("MCP initialize failed for %s: %s (continuing)", self.name, e)
            self.status = "connected"
            self.last_heartbeat = datetime.now(timezone.utc)
            self.last_error = None
            return True
        except Exception as e:
            self.status = "error"
            self.last_error = str(e)[:500]
            logger.warning("MCP connect failed for %s: %s", self.name, e)
            return False

    async def disconnect(self) -> None:
        if self._transport is not None:
            try:
                await self._transport.disconnect()
            except Exception:
                pass
        self._transport = None
        self.status = "disconnected"

    async def heartbeat(self) -> bool:
        """Ping server — returns True if alive."""
        if self.transport == "mock":
            self.last_heartbeat = datetime.now(timezone.utc)
            return True
        transport = self.get_transport()
        if transport is None:
            return True
        # If not connected, try to connect
        if not getattr(transport, "connected", False):
            ok = await self.connect()
            return ok
        try:
            ok = await transport.ping()
            if ok:
                self.last_heartbeat = datetime.now(timezone.utc)
                self.status = "connected"
            else:
                self.status = "error"
            return ok
        except Exception as e:
            self.status = "error"
            self.last_error = str(e)[:500]
            return False

    async def discover_tools(self) -> list[dict]:
        """실제 transport에서 tools/list 호출 — 실패 시 mock 데이터 유지."""
        if self.transport == "mock":
            return self.tool_details or [{"name": t, "server": self.name} for t in self.tools]
        transport = self.get_transport()
        if transport is None:
            return self.tool_details or [{"name": t, "server": self.name} for t in self.tools]
        # Ensure connected
        if not getattr(transport, "connected", False):
            ok = await self.connect()
            if not ok:
                return self.tool_details or [{"name": t, "server": self.name} for t in self.tools]
        try:
            tools = await transport.list_tools()
            if tools:
                self.tool_details = tools
                # Update flat tool names
                self.tools = [t.get("name", "") for t in tools if t.get("name")]
                self.last_heartbeat = datetime.now(timezone.utc)
            return self.tool_details
        except Exception as e:
            logger.warning("discover_tools failed for %s: %s (using cached/mock)", self.name, e)
            self.last_error = str(e)[:500]
            return self.tool_details or [{"name": t, "server": self.name} for t in self.tools]

    async def discover_resources(self) -> list[dict]:
        if self.transport == "mock":
            return self.resource_details or [{"uri": r} for r in self.resources]
        transport = self.get_transport()
        if transport is None:
            return self.resource_details or [{"uri": r} for r in self.resources]
        if not getattr(transport, "connected", False):
            ok = await self.connect()
            if not ok:
                return self.resource_details or [{"uri": r} for r in self.resources]
        try:
            resources = await transport.list_resources()
            if resources:
                self.resource_details = resources
                self.resources = [r.get("uri", "") for r in resources if r.get("uri")]
            return self.resource_details
        except Exception as e:
            logger.warning("discover_resources failed for %s: %s", self.name, e)
            return self.resource_details or [{"uri": r} for r in self.resources]

    async def call_tool(self, tool_name: str, arguments: dict | None = None) -> dict:
        """실제 transport로 tools/call — 실패 시 MCPTransportError."""
        transport = self.get_transport()
        if transport is None or self.transport == "mock":
            # mock fallback: caller should handle mock execution
            from .mcp_transports.base import MCPTransportError
            raise MCPTransportError(f"server {self.name} is mock — no real transport")
        if not getattr(transport, "connected", False):
            ok = await self.connect()
            if not ok:
                from .mcp_transports.base import MCPTransportError
                raise MCPTransportError(f"server {self.name} connect failed: {self.last_error}")
        return await transport.call_tool(tool_name, arguments or {})


class MCPRegistry:
    """MCP Server Registry — tool/resource discovery + normalization + 실연동"""

    def __init__(self):
        self._servers: dict[str, MCPServer] = {}
        # tool_name → server_name 역색인 (빠른 조회)
        self._tool_index: dict[str, str] = {}
        # resource URI → server_name
        self._resource_index: dict[str, str] = {}

    # ── 등록 ──────────────────────────────────────────────────────────

    def register(self, server: MCPServer) -> None:
        """MCP Server 등록 — 기존 동일 name이 있으면 덮어쓴다."""
        # Validate transport
        if server.transport not in ALLOWED_TRANSPORTS:
            # allow but warn
            logger.warning("registering server %s with unknown transport %s", server.name, server.transport)
        # 기존 인덱스 정리
        if server.name in self._servers:
            old = self._servers[server.name]
            for t in old.tools:
                self._tool_index.pop(t, None)
            for r in old.resources:
                self._resource_index.pop(r, None)
            # disconnect old transport if swapping
            if old._transport is not None and old._transport is not server._transport:
                try:
                    # schedule disconnect if we're in event loop, else ignore
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(old.disconnect())
                except RuntimeError:
                    pass
        self._servers[server.name] = server
        for t in server.tools:
            self._tool_index[t] = server.name
        for r in server.resources:
            # resource는 canonical로 정규화하여 인덱싱
            try:
                canon = normalize_resource(r)
            except ValueError:
                canon = r
            self._resource_index[canon] = server.name
            self._resource_index[r] = server.name  # 원본도 유지
        # Also index tool_details
        for td in server.tool_details:
            tname = td.get("name")
            if tname:
                self._tool_index[tname] = server.name

    def unregister(self, server_name: str) -> bool:
        """Server 해제 — 존재하지 않으면 False 반환."""
        srv = self._servers.pop(server_name, None)
        if not srv:
            return False
        for t in srv.tools:
            self._tool_index.pop(t, None)
        for r in srv.resources:
            self._resource_index.pop(r, None)
            try:
                self._resource_index.pop(normalize_resource(r), None)
            except ValueError:
                pass
        for td in srv.tool_details:
            tname = td.get("name") if isinstance(td, dict) else None
            if tname:
                self._tool_index.pop(tname, None)
        # disconnect transport
        if srv._transport is not None:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(srv.disconnect())
            except RuntimeError:
                pass
        return True

    def get_server(self, server_name: str) -> MCPServer | None:
        return self._servers.get(server_name)

    def list_servers(self) -> list[MCPServer]:
        return [s for s in self._servers.values() if s.enabled]

    # ── Tool discovery ───────────────────────────────────────────────

    def list_tools(self) -> list[str]:
        out: list[str] = []
        for s in self._servers.values():
            if s.enabled:
                out.extend(s.tools)
        return out

    def list_tools_detailed(self) -> list[dict]:
        """tool 상세 목록 — GET /v1/tools에서 사용."""
        out: list[dict] = []
        for s in self._servers.values():
            if not s.enabled:
                continue
            if s.tool_details:
                for td in s.tool_details:
                    out.append({"server": s.name, **td})
            else:
                for t in s.tools:
                    out.append({"name": t, "server": s.name, "transport": s.transport})
        return out

    def find_tool(self, tool_name: str) -> MCPServer | None:
        srv_name = self._tool_index.get(tool_name)
        if srv_name:
            return self._servers.get(srv_name)
        # fallback linear search (혹시 인덱스 누락)
        for s in self._servers.values():
            if tool_name in s.tools:
                return s
            # also check tool_details
            for td in s.tool_details:
                if isinstance(td, dict) and td.get("name") == tool_name:
                    return s
        return None

    # ── Resource discovery ───────────────────────────────────────────

    def list_resources(self) -> list[str]:
        out: list[str] = []
        for s in self._servers.values():
            if s.enabled:
                out.extend(s.resources)
        return out

    def list_resources_detailed(self) -> list[dict]:
        out: list[dict] = []
        for s in self._servers.values():
            if not s.enabled:
                continue
            if s.resource_details:
                for rd in s.resource_details:
                    out.append({"server": s.name, **rd})
            else:
                for r in s.resources:
                    try:
                        canon = normalize_resource(r)
                    except ValueError:
                        canon = r
                    out.append({"uri": r, "canonical": canon, "server": s.name})
        return out

    def find_resource(self, resource: str) -> MCPServer | None:
        """resource 문자열로 server 탐색 — canonical 정규화 후 매칭."""
        try:
            canon = normalize_resource(resource)
        except ValueError:
            canon = resource
        srv_name = self._resource_index.get(canon) or self._resource_index.get(resource)
        if srv_name:
            return self._servers.get(srv_name)
        # prefix/wildcard 매칭: resource가 server resource prefix에 속하는지
        for s in self._servers.values():
            for r in s.resources:
                try:
                    r_canon = normalize_resource(r)
                except ValueError:
                    r_canon = r
                # wildcard 지원: gmail/user/* 형태
                if r_canon.endswith("*"):
                    prefix = r_canon.rstrip("*").rstrip("/")
                    if canon.startswith(prefix):
                        return s
                elif canon == r_canon or canon.startswith(r_canon + "/"):
                    return s
        return None

    # ── Normalization helpers ────────────────────────────────────────

    def normalize_tool_call(self, tool_name: str, resource: str, action: str) -> dict[str, str]:
        """tool 호출 시 resource/action 정규화 — execution 전처리에서 사용."""
        try:
            canon_resource = normalize_resource(resource)
        except ValueError:
            canon_resource = resource
        try:
            canon_action = canonicalize_action(action)
        except ValueError:
            canon_action = action.upper().strip()
        return {
            "tool": tool_name,
            "resource": canon_resource,
            "action": canon_action,
        }

    def validate_tool(self, tool_name: str) -> tuple[bool, str]:
        """tool 존재 여부 검증."""
        if self.find_tool(tool_name) is None:
            return False, f"unknown tool: {tool_name}"
        return True, "ok"

    # ── 실연동: connect / discover / heartbeat ───────────────────────

    async def connect_all(self) -> dict[str, bool]:
        """모든 서버에 대해 connect 시도. Returns {server_name: success}."""
        results: dict[str, bool] = {}
        for name, srv in self._servers.items():
            if not srv.enabled:
                continue
            results[name] = await srv.connect()
            # Rebuild index if discovery happened during connect
            self._rebuild_tool_index_for(srv)
        return results

    async def connect_server(self, server_name: str) -> bool:
        srv = self._servers.get(server_name)
        if not srv:
            return False
        ok = await srv.connect()
        if ok:
            self._rebuild_tool_index_for(srv)
        return ok

    async def discover_all(self) -> dict[str, list[dict]]:
        """모든 서버에서 tools/list + resources/list 실제 호출. Mock은 캐시 반환."""
        out: dict[str, list[dict]] = {}
        for name, srv in self._servers.items():
            if not srv.enabled:
                continue
            tools = await srv.discover_tools()
            await srv.discover_resources()
            self._rebuild_tool_index_for(srv)
            out[name] = tools
        return out

    async def discover_server(self, server_name: str) -> list[dict]:
        srv = self._servers.get(server_name)
        if not srv:
            return []
        tools = await srv.discover_tools()
        self._rebuild_tool_index_for(srv)
        return tools

    async def heartbeat_all(self) -> dict[str, bool]:
        """모든 서버 heartbeat (ping). Returns {server_name: alive}."""
        results: dict[str, bool] = {}
        for name, srv in self._servers.items():
            if not srv.enabled:
                continue
            results[name] = await srv.heartbeat()
        return results

    async def heartbeat_server(self, server_name: str) -> bool:
        srv = self._servers.get(server_name)
        if not srv:
            return False
        return await srv.heartbeat()

    async def call_tool(self, tool_name: str, arguments: dict | None = None) -> dict:
        """tool_name으로 서버 찾아 실제 transport로 호출. Mock이면 MCPTransportError."""
        srv = self.find_tool(tool_name)
        if not srv:
            from .mcp_transports.base import MCPTransportError
            raise MCPTransportError(f"unknown tool: {tool_name}")
        return await srv.call_tool(tool_name, arguments)

    def _rebuild_tool_index_for(self, srv: MCPServer) -> None:
        """Rebuild _tool_index for a server after discovery changed its tools."""
        # Remove old entries for this server
        to_del = [k for k, v in self._tool_index.items() if v == srv.name]
        for k in to_del:
            self._tool_index.pop(k, None)
        for t in srv.tools:
            self._tool_index[t] = srv.name
        for td in srv.tool_details:
            if isinstance(td, dict) and td.get("name"):
                self._tool_index[td["name"]] = srv.name

    def get_status(self) -> dict[str, dict]:
        """모든 서버 상태 요약 — health check에서 사용."""
        return {
            name: {
                "transport": srv.transport,
                "status": srv.status,
                "url": srv.url,
                "command": srv.command,
                "tools": len(srv.tools),
                "last_heartbeat": srv.last_heartbeat.isoformat() if srv.last_heartbeat else None,
                "last_error": srv.last_error,
                "enabled": srv.enabled,
            }
            for name, srv in self._servers.items()
        }

    # ── Bootstrap: 기본 서버 등록 ────────────────────────────────────

    def register_defaults(self) -> None:
        """기본 MCP 서버 등록 — Workstream B MVP: google + outline

        환경변수로 실연동 URL 주입 가능:
          MCP_GOOGLE_URL, MCP_OUTLINE_URL, MCP_GOOGLE_COMMAND
        transport 자동 결정: URL이 있으면 streamable-http, 없으면 mock
        """
        google_url = os.getenv("MCP_GOOGLE_URL", "").strip() or None
        outline_url = os.getenv("MCP_OUTLINE_URL", "").strip() or None
        google_cmd = os.getenv("MCP_GOOGLE_COMMAND", "").strip() or None

        # google server — URL이 있으면 streamable-http, command가 있으면 stdio, 없으면 mock fallback
        if google_url:
            g_transport = "streamable-http"
            g_url = google_url
            g_cmd = None
        elif google_cmd:
            g_transport = "stdio"
            g_url = None
            g_cmd = google_cmd
        else:
            # Check legacy file or keep mock for backward compat / test stability
            # If MCP_* env not set, keep mock so tests don't try network
            g_transport = "mock"
            g_url = None
            g_cmd = None

        self.register(MCPServer(
            name="google",
            transport=g_transport,
            url=g_url,
            command=g_cmd,
            tools=[
                "gmail_search", "gmail_read", "gmail_send",
                "calendar_list", "calendar_read", "calendar_create", "calendar_modify",
                "drive_search", "drive_read",
                "tasks_list", "tasks_create", "tasks_modify",
            ],
            resources=[
                "gmail/user/*",
                "calendar/user/*",
                "drive/user/*",
                "tasks/user/*",
            ],
            connector="google",
        ))
        # outline server
        if outline_url:
            o_transport = "streamable-http"
            o_url = outline_url
        else:
            o_transport = "mock"
            o_url = None

        self.register(MCPServer(
            name="outline",
            transport=o_transport,
            url=o_url,
            tools=[
                "outline_search", "outline_read", "outline_create", "outline_modify",
            ],
            resources=[
                "outline/*",
            ],
            connector="outline",
        ))
        # ── mattermost MCP server (agent-to-agent delivery) ──────────
        mm_url = os.getenv("MCP_MATTERMOST_URL", "").strip() or None
        mm_cmd = os.getenv("MCP_MATTERMOST_COMMAND", "").strip() or None
        if mm_url:
            mm_transport = "streamable-http"
            mm_url_val = mm_url
            mm_cmd_val = None
        elif mm_cmd:
            mm_transport = "stdio"
            mm_url_val = None
            mm_cmd_val = mm_cmd
        else:
            mm_transport = "mock"
            mm_url_val = None
            mm_cmd_val = None
        self.register(MCPServer(
            name="mattermost",
            transport=mm_transport,
            url=mm_url_val,
            command=mm_cmd_val,
            tools=[
                "notify_colleague",
                "mattermost_send_direct_message",
                "mattermost_send_dm",
                "mattermost_send_message",
                "mattermost_create_post",
                "mattermost_list_channels",
                "mattermost_get_user",
                "mattermost_search_posts",
            ],
            tool_details=[
                {"name": "notify_colleague", "action": "SEND", "resource_pattern": "mattermost/dm/*", "description": "Send direct message to colleague via Mattermost (agent-to-agent delivery)"},
                {"name": "mattermost_send_direct_message", "action": "SEND", "resource_pattern": "mattermost/dm/*", "description": "Alias for notify_colleague"},
                {"name": "mattermost_send_dm", "action": "SEND", "resource_pattern": "mattermost/dm/*", "description": "Alias for notify_colleague"},
                {"name": "mattermost_send_message", "action": "SEND", "resource_pattern": "mattermost/channel/*"},
                {"name": "mattermost_create_post", "action": "CREATE", "resource_pattern": "mattermost/channel/*"},
                {"name": "mattermost_list_channels", "action": "SEARCH", "resource_pattern": "mattermost/channel/*"},
                {"name": "mattermost_get_user", "action": "READ", "resource_pattern": "mattermost/user/*"},
                {"name": "mattermost_search_posts", "action": "SEARCH", "resource_pattern": "mattermost/channel/*"},
            ],
            resources=[
                "mattermost/channel/*",
                "mattermost/user/*",
                "mattermost/team/*",
                "mattermost/dm/*",
            ],
            connector="mattermost",
        ))

    def register_from_config(self, config: dict) -> None:
        """외부 config(dict)로부터 서버 일괄 등록. config 예시:

        {
          "servers": [
            {"name": "my_stdio", "transport": "stdio", "command": "python -m my_server", "args": [...]},
            {"name": "remote", "transport": "streamable-http", "url": "http://localhost:8001/mcp"},
            {"name": "sse_srv", "transport": "sse", "url": "http://localhost:8002/sse"},
          ]
        }
        """
        for s in config.get("servers", []):
            self.register(MCPServer(
                name=s["name"],
                transport=s.get("transport", "mock"),
                command=s.get("command"),
                url=s.get("url"),
                args=s.get("args"),
                headers=s.get("headers"),
                timeout=s.get("timeout", 30.0),
                tools=s.get("tools", []),
                resources=s.get("resources", []),
                tool_details=s.get("tool_details", []),
                resource_details=s.get("resource_details", []),
                connector=s.get("connector"),
                enabled=s.get("enabled", True),
            ))


# 싱글톤 레지스트리 (app.py에서 import하여 사용)
default_registry = MCPRegistry()
default_registry.register_defaults()
