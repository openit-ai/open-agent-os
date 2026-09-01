"""Microsoft adapter — Graph API (Mail/Calendar/Drive) + Azure AD OAuth.

Mirrors Google adapter's OAuth + vault pattern for Azure AD.

Env:
  MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID, MS_REDIRECT_URI
  (fallback: MICROSOFT_CLIENT_ID etc.)
"""
from __future__ import annotations

import os
import urllib.parse
import uuid
from typing import Any

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore

MS_AUTH_BASE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
MS_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

SCOPES: dict[str, str] = {
    "ms_mail_search": "Mail.Read",
    "ms_mail_read": "Mail.Read",
    "ms_mail_send": "Mail.Send",
    "ms_calendar_list": "Calendars.Read",
    "ms_calendar_read": "Calendars.Read",
    "ms_calendar_create": "Calendars.ReadWrite",
    "ms_drive_search": "Files.Read",
    "ms_drive_read": "Files.Read",
    "ms_tasks_list": "Tasks.Read",
    "ms_tasks_create": "Tasks.ReadWrite",
}

TOOL_ACTION: dict[str, str] = {
    "ms_mail_search": "SEARCH", "ms_mail_read": "READ", "ms_mail_send": "SEND",
    "ms_calendar_list": "SEARCH", "ms_calendar_read": "READ", "ms_calendar_create": "CREATE",
    "ms_drive_search": "SEARCH", "ms_drive_read": "READ",
    "ms_tasks_list": "SEARCH", "ms_tasks_create": "CREATE",
}


class MicrosoftAdapter:
    """Microsoft 365 / Graph adapter — OAuth + vault + Graph skeleton."""

    name = "microsoft"
    provider = "microsoft"

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        tenant_id: str | None = None,
        redirect_uri: str | None = None,
        vault: Any | None = None,
        delegation_service: Any | None = None,
    ) -> None:
        self.client_id = client_id or os.getenv("MS_CLIENT_ID") or os.getenv("MICROSOFT_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("MS_CLIENT_SECRET") or os.getenv("MICROSOFT_CLIENT_SECRET", "")
        self.tenant_id = tenant_id or os.getenv("MS_TENANT_ID") or os.getenv("MICROSOFT_TENANT_ID", "common")
        self.redirect_uri = redirect_uri or os.getenv("MS_REDIRECT_URI") or os.getenv("MICROSOFT_REDIRECT_URI", "http://localhost:8080/oauth/callback")
        self.vault = vault
        self.delegation_service = delegation_service
        self._states: dict[str, dict[str, str]] = {}
        self._binding: dict[str, str] = {}

    def _auth_url(self) -> str:
        return MS_AUTH_BASE.format(tenant=self.tenant_id) + "/authorize"

    def _token_url(self) -> str:
        return MS_AUTH_BASE.format(tenant=self.tenant_id) + "/token"

    # ---- OAuth --------------------------------------------------------------

    def authorize_url(self, delegation_id: str, user_id: str, scopes: list[str] | None = None, state: str | None = None) -> tuple[str, str]:
        st = state or uuid.uuid4().hex
        self._states[st] = {"delegation_id": delegation_id, "user_id": user_id}
        scopes = scopes or ["https://graph.microsoft.com/Mail.Read", "https://graph.microsoft.com/Calendars.Read", "https://graph.microsoft.com/Files.Read"]
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(scopes),
            "state": st,
            "response_mode": "query",
        }
        return f"{self._auth_url()}?{urllib.parse.urlencode(params)}", st

    async def exchange_code(self, code: str, state: str) -> dict[str, Any]:
        st = self._states.get(state)
        if st is None:
            raise ValueError(f"invalid state: {state}")
        if httpx is None:
            raise RuntimeError("httpx not installed")
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
            "scope": "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Calendars.Read",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(self._token_url(), data=data)
            resp.raise_for_status()
            tok = resp.json()
        scope = tok.get("scope", "")
        secret_ref = None
        if self.vault is not None:
            bundle = f"{tok['access_token']}::{tok.get('refresh_token','')}".encode()
            secret_ref = await self.vault.store(st["user_id"], self.provider, scope, bundle)
            self._binding[st["delegation_id"]] = secret_ref
            if self.delegation_service is not None:
                try:
                    self.delegation_service.bind_credential(st["delegation_id"], self.provider, secret_ref, scope)
                except Exception:
                    pass
        self._states.pop(state, None)
        return {"delegation_id": st["delegation_id"], "user_id": st["user_id"], "scope": scope, "secret_ref": secret_ref, "has_refresh_token": bool(tok.get("refresh_token"))}

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        if httpx is None:
            raise RuntimeError("httpx not installed")
        data = {"client_id": self.client_id, "client_secret": self.client_secret, "refresh_token": refresh_token, "grant_type": "refresh_token", "scope": "https://graph.microsoft.com/Mail.Read"}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(self._token_url(), data=data)
            resp.raise_for_status()
            return resp.json()

    # ---- MCP ----------------------------------------------------------------

    def required_scope(self, tool_name: str) -> str | None:
        return SCOPES.get(tool_name)

    def tool_action(self, tool_name: str) -> str:
        return TOOL_ACTION.get(tool_name, "READ")

    async def list_tools(self) -> list[str]:
        return list(SCOPES.keys())

    async def list_resources(self) -> list[str]:
        return ["microsoft/mail/*", "microsoft/calendar/*", "microsoft/drive/*", "microsoft/tasks/*"]

    def describe_tools(self) -> list[dict[str, Any]]:
        return [{"name": k, "action": TOOL_ACTION.get(k, "READ"), "scope": v, "resource_pattern": "microsoft/*"} for k, v in SCOPES.items()]

    async def call_tool(self, tool_name: str, args: dict[str, Any], agent_context: dict[str, Any] | Any, access_token: str | None = None) -> dict[str, Any]:
        # Owner isolation placeholder + skeleton
        ctx_user = agent_context.get("user_id") if isinstance(agent_context, dict) else getattr(agent_context, "user_id", None)
        resource = args.get("resource", "")
        if resource:
            parts = resource.split("/")
            if len(parts) >= 3 and parts[1] == "user":
                owner = f"employee:{parts[2]}"
                if owner != ctx_user:
                    raise PermissionError(f"owner mismatch: {owner} != {ctx_user}")
        if access_token is None:
            if os.getenv("OAOS_ENV", "").strip().lower() in {"production", "prod"}:
                raise RuntimeError("Microsoft Graph access token required in production")
            return {"tool": tool_name, "status": "requires_token", "scope": self.required_scope(tool_name), "resource": resource, "planned": self._planned(tool_name, args)}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        planned = self._planned(tool_name, args)
        method = str(planned.get("method", "GET")).upper()
        path = str(planned.get("path", "/"))
        url = f"{MS_GRAPH_BASE}{path}"
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        request_kwargs: dict[str, Any] = {"headers": headers, "timeout": 15.0}
        if planned.get("params"):
            request_kwargs["params"] = planned["params"]
        if planned.get("json") is not None:
            request_kwargs["json"] = planned["json"]
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.request(method, url, **request_kwargs)
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError:
                data = {"text": response.text}
        return {"tool": tool_name, "resource": resource, "transport": "real", "status_code": response.status_code, "data": data, "scope": self.required_scope(tool_name)}

    def _planned(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        m = {
            "ms_mail_search": {"method": "GET", "path": "/me/messages", "params": {"$search": args.get("q", "")}},
            "ms_mail_read": {"method": "GET", "path": f"/me/messages/{args.get('id','')}"},
            "ms_mail_send": {"method": "POST", "path": "/me/sendMail", "json": args.get("message", {})},
            "ms_calendar_list": {"method": "GET", "path": "/me/calendars"},
            "ms_drive_search": {"method": "GET", "path": "/me/drive/root/search(q='{}')".format(args.get("q",""))},
        }
        return m.get(tool_name, {"method": "GET", "path": "/", "params": args})

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "provider": self.provider, "tools": list(SCOPES.keys()), "resources": ["microsoft/*"], "scopes": SCOPES}
