"""MCP Registry — tool discovery & normalization (Section 7.2)"""
from dataclasses import dataclass, field

@dataclass
class MCPServer:
    name: str
    transport: str  # stdio / sse / streamable-http
    command: str | None = None
    url: str | None = None
    tools: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)

class MCPRegistry:
    def __init__(self):
        self._servers: dict[str, MCPServer] = {}

    def register(self, server: MCPServer) -> None:
        self._servers[server.name] = server

    def list_tools(self) -> list[str]:
        out = []
        for s in self._servers.values():
            out.extend(s.tools)
        return out

    def find_tool(self, tool_name: str) -> MCPServer | None:
        for s in self._servers.values():
            if tool_name in s.tools:
                return s
        return None
