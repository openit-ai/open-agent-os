"""Slack adapter — workspace messaging (specular to Mattermost).

Covers: OAuth, webhook verification, Bot API skeleton, identity mapping §14.

Env:
  SLACK_BOT_TOKEN (xoxb-...), SLACK_SIGNING_SECRET, SLACK_CLIENT_ID/SECRET
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from typing import Any

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore


SLACK_API_BASE = "https://slack.com/api"


class SlackAdapter:
    """Slack workspace adapter — mirrors Mattermost interface for symmetry."""

    name = "slack"
    provider = "slack"

    TOOL_ACTION: dict[str, str] = {
        "slack_send_message": "SEND",
        "slack_post_message": "CREATE",
        "slack_list_channels": "SEARCH",
        "slack_get_user": "READ",
        "slack_search_messages": "SEARCH",
        "slack_create_channel": "CREATE",
        "slack_add_reaction": "CREATE",
    }

    SCOPES: dict[str, str] = {
        "slack_send_message": "chat:write",
        "slack_post_message": "chat:write",
        "slack_list_channels": "channels:read",
        "slack_get_user": "users:read",
        "slack_search_messages": "search:read",
        "slack_create_channel": "channels:write",
        "slack_add_reaction": "reactions:write",
    }

    def __init__(
        self,
        bot_token: str | None = None,
        signing_secret: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        self.bot_token = bot_token or os.getenv("SLACK_BOT_TOKEN") or ""
        self.signing_secret = signing_secret or os.getenv("SLACK_SIGNING_SECRET") or ""
        self.client_id = client_id or os.getenv("SLACK_CLIENT_ID") or ""
        self.client_secret = client_secret or os.getenv("SLACK_CLIENT_SECRET") or ""
        self._identity_map: dict[str, str] = {}
        self._reverse_map: dict[str, str] = {}

    # ---- OAuth (authorization_code) ----------------------------------------

    def authorize_url(self, state: str, scopes: list[str] | None = None, redirect_uri: str | None = None) -> str:
        scopes = scopes or ["chat:write", "channels:read", "users:read"]
        params = {
            "client_id": self.client_id,
            "scope": ",".join(scopes),
            "state": state,
            "redirect_uri": redirect_uri or os.getenv("SLACK_REDIRECT_URI", ""),
        }
        # filter empty
        q = "&".join(f"{k}={v}" for k, v in params.items() if v)
        return f"https://slack.com/oauth/v2/authorize?{q}"

    async def exchange_code(self, code: str, redirect_uri: str | None = None) -> dict[str, Any]:
        if httpx is None:
            raise RuntimeError("httpx not installed")
        data = {"client_id": self.client_id, "client_secret": self.client_secret, "code": code}
        if redirect_uri:
            data["redirect_uri"] = redirect_uri  # type: ignore
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{SLACK_API_BASE}/oauth.v2.access", data=data)
            resp.raise_for_status()
            return resp.json()

    # ---- Identity mapping §14 ----------------------------------------------

    def map_slack_user(self, slack_user_id: str, slack_username: str | None = None) -> str:
        if slack_user_id in self._identity_map:
            return self._identity_map[slack_user_id]
        raw = slack_username or slack_user_id
        suffix = re.sub(r"[^a-z0-9_.-]", "", raw.lower()) or "unknown"
        return f"employee:{suffix}"

    def register_identity(self, slack_user_id: str, employee_principal: str) -> None:
        if not employee_principal.startswith("employee:"):
            raise ValueError("employee_principal must be employee:...")
        self._identity_map[slack_user_id] = employee_principal
        self._reverse_map[employee_principal] = slack_user_id

    def reverse_map(self, employee_principal: str) -> str | None:
        return self._reverse_map.get(employee_principal)

    # ---- Webhook / Events verification (Slack Signing Secret) ---------------

    def verify_signature(self, body: bytes, timestamp: str | None, signature: str | None) -> bool:
        if not self.signing_secret:
            return True
        if not timestamp or not signature:
            return False
        # replay window 5min
        try:
            if abs(time.time() - int(timestamp)) > 300:
                return False
        except ValueError:
            return False
        basestring = f"v0:{timestamp}:{body.decode()}"
        expected = "v0=" + hmac.new(self.signing_secret.encode(), basestring.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_incoming(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = payload.get("event", payload)
        user_id = event.get("user") or payload.get("user_id") or ""
        username = event.get("username") or ""
        text = event.get("text") or payload.get("text") or ""
        channel = event.get("channel") or payload.get("channel_id") or ""
        employee = self.map_slack_user(user_id, username)
        return {"slack_user_id": user_id, "employee_principal": employee, "text": text, "channel": channel, "raw": payload}

    # ---- Bot API skeleton ---------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.bot_token}", "Content-Type": "application/json"} if self.bot_token else {}

    async def send_message(self, channel: str, text: str, **kwargs: Any) -> dict[str, Any]:
        if not self.bot_token:
            return {"_skeleton": True, "channel": channel, "text": text, "message": "SLACK_BOT_TOKEN not set"}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        body: dict[str, Any] = {"channel": channel, "text": text}
        body.update(kwargs)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{SLACK_API_BASE}/chat.postMessage", json=body, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def list_channels(self) -> dict[str, Any]:
        if not self.bot_token:
            return {"_skeleton": True, "message": "SLACK_BOT_TOKEN not set"}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{SLACK_API_BASE}/conversations.list", headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    # ---- MCP registry -------------------------------------------------------

    def required_scope(self, tool_name: str) -> str | None:
        return self.SCOPES.get(tool_name)

    def tool_action(self, tool_name: str) -> str:
        return self.TOOL_ACTION.get(tool_name, "READ")

    async def list_tools(self) -> list[str]:
        return list(self.TOOL_ACTION.keys())

    async def list_resources(self) -> list[str]:
        return ["slack/channel/*", "slack/user/*", "slack/workspace/*"]

    def describe_tools(self) -> list[dict[str, Any]]:
        return [{"name": k, "action": v, "scope": self.SCOPES.get(k), "resource_pattern": "slack/*"} for k, v in self.TOOL_ACTION.items()]

    async def call_tool(self, tool_name: str, args: dict[str, Any], agent_context: dict[str, Any] | Any) -> dict[str, Any]:
        if tool_name in ("slack_send_message", "slack_post_message"):
            return await self.send_message(args.get("channel") or args.get("channel_id", ""), args.get("text") or args.get("message", ""), **{k: v for k, v in args.items() if k not in ("channel", "channel_id", "text", "message")})
        if tool_name == "slack_list_channels":
            return await self.list_channels()
        if tool_name == "slack_get_user":
            return {"_skeleton": True, "user": args.get("user_id")}
        if tool_name == "slack_search_messages":
            return {"_skeleton": True, "query": args.get("query", "")}
        raise ValueError(f"unknown tool: {tool_name}")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "provider": self.provider, "tools": list(self.TOOL_ACTION.keys()), "resources": ["slack/*"], "has_bot_token": bool(self.bot_token), "scopes": self.SCOPES}
