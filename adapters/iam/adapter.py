"""IAM adapter — identity provider sync (Google Workspace / Azure AD).

Expands interface to google/outline level: concrete method signatures for
user/group sync, principal mapping §14, and tenant isolation.

Env:
  IAM_PROVIDER (google|azure|okta), IAM_DOMAIN, IAM_API_KEY / IAM_CREDENTIALS_JSON
"""
from __future__ import annotations

import os
import re
from typing import Any

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore


class IamAdapter:
    """IAM / directory adapter — user/group sync + principal mapping (§14)."""

    name = "iam"
    provider = "iam"

    TOOL_ACTION: dict[str, str] = {
        "iam_get_user": "READ",
        "iam_list_users": "SEARCH",
        "iam_get_group": "READ",
        "iam_list_groups": "SEARCH",
        "iam_sync_users": "SYNC",
        "iam_resolve_principal": "READ",
    }

    def __init__(
        self,
        provider: str | None = None,
        domain: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.iam_provider = (provider or os.getenv("IAM_PROVIDER") or "google").lower()
        self.domain = domain or os.getenv("IAM_DOMAIN") or ""
        self.api_key = api_key or os.getenv("IAM_API_KEY") or ""
        # local cache (skeleton)
        self._users: dict[str, dict[str, Any]] = {}
        self._groups: dict[str, list[str]] = {}

    # ---- Principal mapping §14 ----------------------------------------------

    def to_employee_principal(self, email: str) -> str:
        """email -> employee: principal (canonical)."""
        local = email.split("@")[0].lower()
        suffix = re.sub(r"[^a-z0-9_.-]", "", local) or "unknown"
        return f"employee:{suffix}"

    def to_agent_principal(self, employee_principal: str) -> str:
        if not employee_principal.startswith("employee:"):
            raise ValueError("employee_principal must start with employee:")
        return employee_principal.replace("employee:", "agent:assistant:", 1)

    def resolve_principal(self, email_or_id: str) -> dict[str, str]:
        emp = self.to_employee_principal(email_or_id) if "@" in email_or_id else f"employee:{email_or_id}"
        return {"employee_principal": emp, "agent_principal": self.to_agent_principal(emp), "provider": self.iam_provider}

    # ---- Directory operations (skeleton, httpx when configured) --------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def get_user(self, user_id: str) -> dict[str, Any]:
        if user_id in self._users:
            return self._users[user_id]
        if not self.api_key:
            return {"_skeleton": True, "user_id": user_id, "principal": self.to_employee_principal(user_id), "message": "IAM_API_KEY not set"}
        # Real: Google Directory API GET /admin/directory/v1/users/{userKey}
        return {"_skeleton": True, "user_id": user_id}

    async def list_users(self, domain: str | None = None, max_results: int = 100) -> dict[str, Any]:
        if not self.api_key:
            users = list(self._users.values())[:max_results]
            return {"_skeleton": True, "users": users, "count": len(users), "message": "IAM_API_KEY not set — returning local cache"}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        # Placeholder for real directory API
        return {"users": [], "count": 0}

    async def get_group(self, group_id: str) -> dict[str, Any]:
        members = self._groups.get(group_id, [])
        if members:
            return {"group_id": group_id, "members": members}
        if not self.api_key:
            return {"_skeleton": True, "group_id": group_id, "message": "IAM_API_KEY not set"}
        return {"group_id": group_id, "members": []}

    async def list_groups(self, domain: str | None = None) -> dict[str, Any]:
        if not self.api_key:
            return {"_skeleton": True, "groups": list(self._groups.keys()), "message": "IAM_API_KEY not set"}
        return {"groups": []}

    async def sync_users(self, users: list[dict[str, Any]]) -> dict[str, Any]:
        """Bulk upsert users into local cache (called by admin sync job)."""
        count = 0
        for u in users:
            uid = u.get("id") or u.get("email") or u.get("principal", "")
            if uid:
                self._users[uid] = u
                count += 1
        return {"synced": count, "total": len(self._users)}

    async def sync_groups(self, groups: dict[str, list[str]]) -> dict[str, Any]:
        self._groups.update(groups)
        return {"synced": len(groups), "total": len(self._groups)}

    # ---- MCP registry --------------------------------------------------------

    def required_scope(self, tool_name: str) -> str | None:
        return {"iam_get_user": "directory.read", "iam_list_users": "directory.read", "iam_sync_users": "directory.write"}.get(tool_name)

    def tool_action(self, tool_name: str) -> str:
        return self.TOOL_ACTION.get(tool_name, "READ")

    async def list_tools(self) -> list[str]:
        return list(self.TOOL_ACTION.keys())

    async def list_resources(self) -> list[str]:
        return ["iam/user/*", "iam/group/*", "iam/tenant/*"]

    def describe_tools(self) -> list[dict[str, Any]]:
        return [{"name": k, "action": v, "resource_pattern": "iam/*"} for k, v in self.TOOL_ACTION.items()]

    async def call_tool(self, tool_name: str, args: dict[str, Any], agent_context: dict[str, Any] | Any) -> dict[str, Any]:
        if tool_name == "iam_get_user":
            return await self.get_user(args.get("user_id") or args.get("email", ""))
        if tool_name == "iam_list_users":
            return await self.list_users(domain=args.get("domain"), max_results=int(args.get("max_results", 100)))
        if tool_name == "iam_get_group":
            return await self.get_group(args.get("group_id", ""))
        if tool_name == "iam_list_groups":
            return await self.list_groups(domain=args.get("domain"))
        if tool_name == "iam_sync_users":
            return await self.sync_users(args.get("users", []))
        if tool_name == "iam_resolve_principal":
            return self.resolve_principal(args.get("email") or args.get("user_id", ""))
        raise ValueError(f"unknown tool: {tool_name}")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "provider": self.iam_provider, "tools": list(self.TOOL_ACTION.keys()), "resources": ["iam/*"], "domain": self.domain, "has_api_key": bool(self.api_key)}
