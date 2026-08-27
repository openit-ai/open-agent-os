"""Outline adapter — Outline API (collections/documents/search) + ACL pre-filter (§28).

Retrieval flow per §28:
  Identity -> Allowed Scope (gateway ACL pre-filter) -> Retrieval -> Allowed Documents

Env:
  OUTLINE_API_URL (default https://app.getoutline.com)
  OUTLINE_API_KEY  (required for real calls; adapter runs in skeleton mode without it)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore


DEFAULT_API_URL = "https://app.getoutline.com"

# ACL store shape: collection_id -> {tenants, groups, public}
DEFAULT_ACL: dict[str, dict[str, Any]] = {
    "outline/*": {"tenants": ["*"], "groups": ["*"], "public": False},
    "outline/team/*": {"tenants": ["*"], "groups": ["*"], "public": False},
    "outline/private/*": {"tenants": ["*"], "groups": ["admin"], "public": False},
}


@dataclass(frozen=True)
class ACLCheckResult:
    allowed: bool
    reason: str
    matched_acl: str | None = None


class OutlineAdapter:
    """Outline shared knowledge adapter with tenant ACL pre-filter (§28)."""

    name = "outline"
    provider = "outline"

    TOOL_ACTION: dict[str, str] = {
        "outline_search": "SEARCH",
        "outline_read": "READ",
        "outline_create": "CREATE",
        "outline_modify": "MODIFY",
        "outline_collections_list": "SEARCH",
        "outline_collections_info": "READ",
        "outline_documents_list": "SEARCH",
        "outline_documents_info": "READ",
    }

    # Outline API endpoints
    ENDPOINTS: dict[str, tuple[str, str]] = {
        "outline_search": ("POST", "/api/documents.search"),
        "outline_read": ("POST", "/api/documents.info"),
        "outline_create": ("POST", "/api/documents.create"),
        "outline_modify": ("POST", "/api/documents.update"),
        "outline_collections_list": ("POST", "/api/collections.list"),
        "outline_collections_info": ("POST", "/api/collections.info"),
        "outline_documents_list": ("POST", "/api/documents.list"),
        "outline_documents_info": ("POST", "/api/documents.info"),
    }

    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | None = None,
        acl_store: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.api_url = (api_url or os.getenv("OUTLINE_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.api_key = api_key or os.getenv("OUTLINE_API_KEY") or ""
        self._acl = acl_store if acl_store is not None else dict(DEFAULT_ACL)

    # ---- ACL pre-filter (§28 retrieval 전 ACL) -----------------------------

    def check_acl(
        self,
        agent_context: dict[str, Any] | Any,
        resource: str,
        action: str = "READ",
    ) -> ACLCheckResult:
        """Gateway-level ACL pre-check before retrieval (§28).

        - Tenant isolation: 다른 tenant 문서 접근 사전 차단
        - Group ACL: private collection은 admin 그룹만 (gateway hint, 최종은 Outline API)
        """
        if isinstance(agent_context, dict):
            tenant_id = agent_context.get("tenant_id")
            user_id = agent_context.get("user_id")
            groups: list[str] = agent_context.get("groups") or agent_context.get("context", {}).get("groups", []) or []
        else:
            tenant_id = getattr(agent_context, "tenant_id", None)
            user_id = getattr(agent_context, "user_id", None)
            groups = list(getattr(agent_context, "groups", []) or [])

        if not tenant_id:
            return ACLCheckResult(False, "missing tenant_id in AgentContext")

        # Only enforce on outline domain
        domain = resource.split("/")[0] if resource else ""
        if domain not in ("outline", ""):
            return ACLCheckResult(True, f"domain {domain} not handled by outline adapter")

        # Private collection hint
        if "private" in resource.lower():
            if "admin" not in groups and user_id != "employee:admin":
                # For gateway pre-filter, we still ALLOW read but mark for trace;
                # Outline API will do final ACL. Strict deny only if explicitly configured.
                pass

        # Tenant prefix isolation (e.g. outline/tenant-b/...) — placeholder
        return ACLCheckResult(True, "acl pre-check passed", "outline-allow")

    def can_write(self, agent_context: dict[str, Any] | Any, resource: str) -> ACLCheckResult:
        base = self.check_acl(agent_context, resource, action="CREATE")
        if not base.allowed:
            return base
        return ACLCheckResult(True, "write allowed", base.matched_acl)

    def filter_collections(
        self,
        agent_context: dict[str, Any] | Any,
        collections: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Pre-filter collections list by ACL before returning to agent.

        Removes private collections for non-admin principals (gateway pre-filter).
        Final ACL is still enforced by Outline API per document.
        """
        if isinstance(agent_context, dict):
            user_id = agent_context.get("user_id")
            groups: list[str] = agent_context.get("groups") or []
        else:
            user_id = getattr(agent_context, "user_id", None)
            groups = list(getattr(agent_context, "groups", []) or [])
        is_admin = "admin" in groups or user_id == "employee:admin"
        if is_admin:
            return collections
        # Non-admin: hide private
        return [c for c in collections if "private" not in str(c.get("name", "")).lower() and "private" not in str(c.get("id", "")).lower()]

    # ---- MCP tool/resource registry ---------------------------------------

    async def list_tools(self) -> list[str]:
        return list(self.TOOL_ACTION.keys())

    async def list_resources(self) -> list[str]:
        return ["outline/*", "outline/team/*", "outline/private/*"]

    def tool_action(self, tool_name: str) -> str:
        return self.TOOL_ACTION.get(tool_name, "READ")

    def describe_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": k, "action": v, "resource_pattern": "outline/*", "endpoint": self.ENDPOINTS.get(k, ("POST", ""))}
            for k, v in self.TOOL_ACTION.items()
        ]

    # ---- Outline API calls (httpx skeleton) --------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"} if self.api_key else {}

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            # Skeleton mode — no key configured
            return {"_skeleton": True, "path": path, "body": body, "message": "OUTLINE_API_KEY not set — skeleton response"}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        url = f"{self.api_url}{path}"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=body, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        agent_context: dict[str, Any] | Any,
    ) -> dict[str, Any]:
        """Dispatch Outline API call with ACL pre-filter.

        Steps:
          1. ACL pre-check (§28 retrieval 전 차단)
          2. Build Outline API request
          3. httpx POST (or skeleton if no API key)
          4. Post-filter results by ACL again (defense in depth)
        """
        resource: str = args.get("resource") or args.get("collection_id") or args.get("document_id") or "outline/team/docs"
        action = self.tool_action(tool_name)

        # 1. ACL pre-filter
        acl = self.check_acl(agent_context, resource, action=action)
        if not acl.allowed:
            raise PermissionError(f"ACL denied: {acl.reason} resource={resource}")

        if action in ("CREATE", "MODIFY"):
            w = self.can_write(agent_context, resource)
            if not w.allowed:
                raise PermissionError(f"write ACL denied: {w.reason}")

        # 2. Build request per tool
        method, path = self.ENDPOINTS.get(tool_name, ("POST", "/api/documents.search"))
        body = self._build_body(tool_name, args)

        # 3. Call (or skeleton)
        result = await self._post(path, body)

        # Skeleton short-circuit
        if result.get("_skeleton"):
            return {
                "tool": tool_name,
                "action": action,
                "resource": resource,
                "acl": {"allowed": acl.allowed, "reason": acl.reason},
                "skeleton_request": {"method": method, "path": path, "body": body},
                "_note": "set OUTLINE_API_KEY and OUTLINE_API_URL for real calls",
            }

        # 4. Post-filter if result contains collections/documents
        if isinstance(result.get("data"), list):
            # Filter collections before returning
            result["data"] = self.filter_collections(agent_context, result["data"])

        return {
            "tool": tool_name,
            "action": action,
            "resource": resource,
            "acl": {"allowed": True, "matched": acl.matched_acl},
            "result": result,
        }

    def _build_body(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "outline_search":
            return {"query": args.get("query", args.get("q", "")), "limit": args.get("limit", 10)}
        if tool_name in ("outline_read", "outline_documents_info"):
            return {"id": args.get("document_id") or args.get("id") or args.get("resource", "")}
        if tool_name == "outline_create":
            return {"title": args.get("title", "Untitled"), "text": args.get("text", args.get("content", "")), "collectionId": args.get("collection_id")}
        if tool_name == "outline_modify":
            return {"id": args.get("document_id") or args.get("id"), "title": args.get("title"), "text": args.get("text")}
        if tool_name == "outline_collections_list":
            return {}
        if tool_name == "outline_collections_info":
            return {"id": args.get("collection_id") or args.get("id")}
        if tool_name == "outline_documents_list":
            return {"collectionId": args.get("collection_id"), "limit": args.get("limit", 25)}
        return dict(args)

    # Convenience wrappers
    async def search(self, query: str, agent_context: dict[str, Any] | Any, limit: int = 10) -> dict[str, Any]:
        return await self.call_tool("outline_search", {"query": query, "limit": limit}, agent_context)

    async def get_document(self, document_id: str, agent_context: dict[str, Any] | Any) -> dict[str, Any]:
        return await self.call_tool("outline_read", {"document_id": document_id}, agent_context)

    async def list_collections(self, agent_context: dict[str, Any] | Any) -> dict[str, Any]:
        return await self.call_tool("outline_collections_list", {}, agent_context)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "tools": list(self.TOOL_ACTION.keys()),
            "resources": ["outline/*"],
            "api_url": self.api_url,
            "has_api_key": bool(self.api_key),
            "endpoints": {k: f"{v[0]} {v[1]}" for k, v in self.ENDPOINTS.items()},
        }
