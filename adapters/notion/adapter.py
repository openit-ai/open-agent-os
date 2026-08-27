"""Notion adapter — Notion API (databases/pages/search) + ACL pre-filter.

Mirrors Outline adapter's ACL pattern (§28) for shared knowledge.

Env:
  NOTION_API_KEY (secret_...), NOTION_API_URL (default https://api.notion.com)
"""
from __future__ import annotations

import os
from typing import Any

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

TOOL_ACTION: dict[str, str] = {
    "notion_search": "SEARCH",
    "notion_read_page": "READ",
    "notion_read_database": "READ",
    "notion_create_page": "CREATE",
    "notion_update_page": "MODIFY",
    "notion_query_database": "SEARCH",
    "notion_list_databases": "SEARCH",
}

SCOPES: dict[str, str] = {k: "notion:read" if "read" in k or "search" in k or "query" in k or "list" in k else "notion:write" for k in TOOL_ACTION}


class NotionAdapter:
    """Notion shared knowledge adapter — ACL pre-filter + API skeleton."""

    name = "notion"
    provider = "notion"

    NOTION_HEADERS = {"Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}

    def __init__(self, api_key: str | None = None, api_url: str | None = None) -> None:
        self.api_key = api_key or os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN", "")
        self.api_url = (api_url or os.getenv("NOTION_API_URL") or NOTION_API_BASE).rstrip("/")

    # ---- ACL pre-filter (§28) ------------------------------------------------
    def check_acl(self, agent_context: dict[str, Any] | Any, resource: str, action: str = "READ") -> dict[str, Any]:
        tenant_id = agent_context.get("tenant_id") if isinstance(agent_context, dict) else getattr(agent_context, "tenant_id", None)
        if not tenant_id:
            return {"allowed": False, "reason": "missing tenant_id"}
        domain = resource.split("/")[0] if resource else ""
        if domain not in ("notion", ""):
            return {"allowed": True, "reason": f"domain {domain} not handled"}
        return {"allowed": True, "reason": "acl pre-check passed"}

    # ---- MCP ----------------------------------------------------------------

    def required_scope(self, tool_name: str) -> str | None:
        return SCOPES.get(tool_name)

    def tool_action(self, tool_name: str) -> str:
        return TOOL_ACTION.get(tool_name, "READ")

    async def list_tools(self) -> list[str]:
        return list(TOOL_ACTION.keys())

    async def list_resources(self) -> list[str]:
        return ["notion/*", "notion/database/*", "notion/page/*"]

    def describe_tools(self) -> list[dict[str, Any]]:
        return [{"name": k, "action": v, "scope": SCOPES.get(k), "resource_pattern": "notion/*"} for k, v in TOOL_ACTION.items()]

    def _headers(self) -> dict[str, str]:
        h = dict(self.NOTION_HEADERS)
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            return {"_skeleton": True, "path": path, "body": body, "message": "NOTION_API_KEY not set"}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        url = f"{self.api_url}{path}"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=body, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def _get(self, path: str) -> dict[str, Any]:
        if not self.api_key:
            return {"_skeleton": True, "path": path, "message": "NOTION_API_KEY not set"}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        url = f"{self.api_url}{path}"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def call_tool(self, tool_name: str, args: dict[str, Any], agent_context: dict[str, Any] | Any) -> dict[str, Any]:
        resource = args.get("resource") or args.get("page_id") or args.get("database_id") or "notion/*"
        acl = self.check_acl(agent_context, resource, action=self.tool_action(tool_name))
        if not acl["allowed"]:
            raise PermissionError(f"ACL denied: {acl['reason']}")
        if tool_name == "notion_search":
            result = await self._post("/search", {"query": args.get("query", args.get("q", "")), "page_size": args.get("limit", 10)})
        elif tool_name == "notion_read_page":
            pid = args.get("page_id") or args.get("id")
            result = await self._get(f"/pages/{pid}")
        elif tool_name == "notion_read_database":
            did = args.get("database_id") or args.get("id")
            result = await self._get(f"/databases/{did}")
        elif tool_name == "notion_query_database":
            did = args.get("database_id")
            result = await self._post(f"/databases/{did}/query", {"filter": args.get("filter"), "sorts": args.get("sorts"), "page_size": args.get("page_size", 10)})
        elif tool_name == "notion_create_page":
            result = await self._post("/pages", {"parent": args.get("parent", {}), "properties": args.get("properties", {})})
        elif tool_name == "notion_update_page":
            pid = args.get("page_id")
            result = await self._post(f"/pages/{pid}", {"properties": args.get("properties", {})}) if False else await self._patch_page(pid, args)  # type: ignore
            # fallback skeleton handled by _post absence
        elif tool_name == "notion_list_databases":
            result = await self._post("/search", {"filter": {"property": "object", "value": "database"}})
        else:
            raise ValueError(f"unknown tool: {tool_name}")

        if isinstance(result, dict) and result.get("_skeleton"):
            return {"tool": tool_name, "resource": resource, "acl": acl, "skeleton_request": result, "_note": "set NOTION_API_KEY for real calls"}
        return {"tool": tool_name, "resource": resource, "acl": acl, "result": result}

    async def _patch_page(self, page_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            return {"_skeleton": True, "path": f"/pages/{page_id}", "body": args}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        url = f"{self.api_url}/pages/{page_id}"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.patch(url, json={"properties": args.get("properties", {})}, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "provider": self.provider, "tools": list(TOOL_ACTION.keys()), "resources": ["notion/*"], "has_api_key": bool(self.api_key)}
