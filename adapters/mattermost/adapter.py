"""Mattermost adapter — webhook + API (§14 identity mapping).

Features:
  - Incoming webhook HMAC verification (X-Mattermost-Signature)
  - Outgoing message via Mattermost Bot API (httpx)
  - User identity mapping §14: mattermost_user_id -> employee: principal

Env:
  MATTERMOST_URL  (e.g. https://mattermost.example.com)
  MATTERMOST_BOT_TOKEN  (Bot access token)
  MATTERMOST_WEBHOOK_SECRET  (HMAC secret for incoming webhook)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Any

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore


class MattermostAdapter:
    """Mattermost webhook + API adapter with identity mapping (§14)."""

    name = "mattermost"
    provider = "mattermost"

    TOOL_ACTION: dict[str, str] = {
        "mattermost_send_message": "SEND",
        "mattermost_create_post": "CREATE",
        "mattermost_list_channels": "SEARCH",
        "mattermost_get_user": "READ",
        "mattermost_search_posts": "SEARCH",
    }

    def __init__(
        self,
        base_url: str | None = None,
        bot_token: str | None = None,
        webhook_secret: str | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("MATTERMOST_URL") or "").rstrip("/")
        self.bot_token = bot_token or os.getenv("MATTERMOST_BOT_TOKEN") or ""
        self.webhook_secret = webhook_secret or os.getenv("MATTERMOST_WEBHOOK_SECRET") or ""
        # identity map: mattermost user id / username -> employee principal
        # In production this is backed by IAM / DB; here in-memory for skeleton
        self._identity_map: dict[str, str] = {}
        # reverse
        self._reverse_map: dict[str, str] = {}

    # ---- Identity mapping §14 ----------------------------------------------

    def map_mattermost_user(self, mm_user_id: str, mm_username: str | None = None) -> str:
        """Mattermost user -> employee: principal (§14 1인 1 Logical Agent).

        Rules:
          - If explicit mapping exists, use it
          - Else derive: employee:<username or user_id suffix>
          - Validate namespace prefix
        """
        if mm_user_id in self._identity_map:
            return self._identity_map[mm_user_id]
        if mm_username and mm_username in self._identity_map:
            return self._identity_map[mm_username]
        # Derive
        raw = mm_username or mm_user_id
        # sanitize: lowercase, strip special
        suffix = re.sub(r"[^a-z0-9_.-]", "", raw.lower()) or "unknown"
        principal = f"employee:{suffix}"
        return principal

    def register_identity(self, mm_user_id: str, employee_principal: str) -> None:
        """Register explicit mapping (called during IAM sync)."""
        if not employee_principal.startswith("employee:"):
            raise ValueError("employee_principal must start with 'employee:'")
        self._identity_map[mm_user_id] = employee_principal
        self._reverse_map[employee_principal] = mm_user_id

    def reverse_map(self, employee_principal: str) -> str | None:
        """employee: -> mattermost user id (for outgoing)."""
        return self._reverse_map.get(employee_principal)

    # ---- Incoming webhook verification --------------------------------------

    def verify_signature(self, body: bytes, signature: str | None) -> bool:
        """Verify HMAC-SHA256 signature for incoming webhook.

        Header: X-Mattermost-Signature = hex(hmac_sha256(secret, body))
        If no secret configured, accept (dev mode).
        """
        if not self.webhook_secret:
            return True
        if not signature:
            return False
        expected = hmac.new(self.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_incoming(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalize incoming webhook payload to canonical event."""
        # Mattermost outgoing webhook / slash command / event payloads vary
        user_id = payload.get("user_id") or payload.get("user", {}).get("id") or ""
        username = payload.get("user_name") or payload.get("user", {}).get("username") or ""
        text = payload.get("text") or payload.get("message") or payload.get("data", {}).get("post", {}).get("message", "") or ""
        channel_id = payload.get("channel_id") or payload.get("channel", {}).get("id") or ""
        team_id = payload.get("team_id") or ""
        employee = self.map_mattermost_user(user_id, username)
        return {
            "mattermost_user_id": user_id,
            "mattermost_username": username,
            "employee_principal": employee,
            "text": text,
            "channel_id": channel_id,
            "team_id": team_id,
            "raw": payload,
        }

    # ---- Outgoing via Bot API (httpx skeleton) ------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.bot_token}", "Content-Type": "application/json"} if self.bot_token else {}

    async def send_message(
        self,
        channel_id: str,
        text: str,
        props: dict[str, Any] | None = None,
        root_id: str | None = None,
    ) -> dict[str, Any]:
        """Send message to Mattermost channel via Bot API.

        POST /api/v4/posts  {channel_id, message, props, root_id}
        Skeleton if no base_url/bot_token configured.
        """
        if not self.base_url or not self.bot_token:
            return {
                "_skeleton": True,
                "channel_id": channel_id,
                "text": text,
                "message": "MATTERMOST_URL or MATTERMOST_BOT_TOKEN not set — skeleton",
            }
        if httpx is None:
            raise RuntimeError("httpx not installed")
        url = f"{self.base_url}/api/v4/posts"
        body: dict[str, Any] = {"channel_id": channel_id, "message": text}
        if props:
            body["props"] = props
        if root_id:
            body["root_id"] = root_id
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=body, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def create_post(self, channel_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        return await self.send_message(channel_id, message, **kwargs)

    async def get_user(self, user_id: str) -> dict[str, Any]:
        if not self.base_url or not self.bot_token:
            return {"_skeleton": True, "user_id": user_id}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        url = f"{self.base_url}/api/v4/users/{user_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def list_channels(self, team_id: str) -> dict[str, Any]:
        if not self.base_url or not self.bot_token:
            return {"_skeleton": True, "team_id": team_id}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        url = f"{self.base_url}/api/v4/teams/{team_id}/channels"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    # ---- MCP registry -------------------------------------------------------

    async def list_tools(self) -> list[str]:
        return list(self.TOOL_ACTION.keys())

    async def list_resources(self) -> list[str]:
        return ["mattermost/channel/*", "mattermost/user/*", "mattermost/team/*"]

    def tool_action(self, tool_name: str) -> str:
        return self.TOOL_ACTION.get(tool_name, "READ")

    def describe_tools(self) -> list[dict[str, Any]]:
        return [{"name": k, "action": v, "resource_pattern": "mattermost/*"} for k, v in self.TOOL_ACTION.items()]

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        agent_context: dict[str, Any] | Any,
    ) -> dict[str, Any]:
        """Dispatch Mattermost tool with identity-aware checks."""
        if tool_name in ("mattermost_send_message", "mattermost_create_post"):
            channel_id = args.get("channel_id") or args.get("channel") or ""
            text = args.get("text") or args.get("message") or ""
            if not channel_id or not text:
                raise ValueError("channel_id and text/message required")
            return await self.send_message(channel_id, text, props=args.get("props"), root_id=args.get("root_id"))
        if tool_name == "mattermost_get_user":
            return await self.get_user(args.get("user_id", ""))
        if tool_name == "mattermost_list_channels":
            return await self.list_channels(args.get("team_id", ""))
        if tool_name == "mattermost_search_posts":
            # skeleton search
            return {"_skeleton": True, "query": args.get("query", ""), "message": "search via /api/v4/posts/search"}
        raise ValueError(f"unknown tool: {tool_name}")

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "tools": list(self.TOOL_ACTION.keys()),
            "resources": ["mattermost/channel/*", "mattermost/user/*"],
            "base_url": self.base_url,
            "has_bot_token": bool(self.bot_token),
            "has_webhook_secret": bool(self.webhook_secret),
        }
