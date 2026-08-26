"""MCP Registry — Section 7.2 고도화

- MCP Server 등록/해제
- tool / resource discovery
- resource/action normalization 연동
- mcp_resource_model 연동

한국어 주석: 설계 원칙 — MCP Registry는 discovery만 담당, 권한 판단은 PolicyEngine/authz_hook에서 수행
"""
from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass
class MCPServer:
    name: str
    transport: str  # stdio / sse / streamable-http
    command: str | None = None
    url: str | None = None
    tools: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    # 확장: 상세 MCPTool / MCPResource 객체
    tool_details: list[dict] = field(default_factory=list)
    resource_details: list[dict] = field(default_factory=list)
    # connector 바인딩 (google, outline 등)
    connector: str | None = None
    enabled: bool = True


class MCPRegistry:
    """MCP Server Registry — tool/resource discovery + normalization"""

    def __init__(self):
        self._servers: dict[str, MCPServer] = {}
        # tool_name → server_name 역색인 (빠른 조회)
        self._tool_index: dict[str, str] = {}
        # resource URI → server_name
        self._resource_index: dict[str, str] = {}

    # ── 등록 ──────────────────────────────────────────────────────────

    def register(self, server: MCPServer) -> None:
        """MCP Server 등록 — 기존 동일 name이 있으면 덮어쓴다."""
        # 기존 인덱스 정리
        if server.name in self._servers:
            old = self._servers[server.name]
            for t in old.tools:
                self._tool_index.pop(t, None)
            for r in old.resources:
                self._resource_index.pop(r, None)
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

    # ── Bootstrap: 기본 서버 등록 ────────────────────────────────────

    def register_defaults(self) -> None:
        """기본 MCP 서버 등록 — Workstream B MVP: google + outline"""
        # google personal connector
        self.register(MCPServer(
            name="google",
            transport="streamable-http",
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
        # outline shared knowledge
        self.register(MCPServer(
            name="outline",
            transport="streamable-http",
            tools=[
                "outline_search", "outline_read", "outline_create", "outline_modify",
            ],
            resources=[
                "outline/*",
            ],
            connector="outline",
        ))


# 싱글톤 레지스트리 (app.py에서 import하여 사용)
default_registry = MCPRegistry()
default_registry.register_defaults()
