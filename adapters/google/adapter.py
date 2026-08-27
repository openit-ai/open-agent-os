"""Google adapter — OAuth 2.0 authorization_code + Vault, Gmail/Calendar/Drive/Tasks.

Section 9-10: Personal Delegation / Vault binding (delegation_id)
Section 17: ACP context, Section 28: Knowledge not applicable here (personal tools)

Flow:
  authorize_url -> user consent -> exchange_code -> Vault store + Delegation bind
  refresh -> rotate access_token via refresh_token
  revoke -> revoke token + delegation cascade
  list_tools / call_tool -> httpx skeleton with googleapis minimal scopes

Env:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
  GOOGLE_SCOPES (optional override), VAULT_ENCRYPTION_KEY (via vault)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Minimal scopes per tool — least privilege (Section 10.2)
GOOGLE_SCOPES: dict[str, str] = {
    "gmail_search": "https://www.googleapis.com/auth/gmail.readonly",
    "gmail_read": "https://www.googleapis.com/auth/gmail.readonly",
    "gmail_send": "https://www.googleapis.com/auth/gmail.send",
    "calendar_list": "https://www.googleapis.com/auth/calendar.readonly",
    "calendar_read": "https://www.googleapis.com/auth/calendar.readonly",
    "calendar_create": "https://www.googleapis.com/auth/calendar",
    "calendar_modify": "https://www.googleapis.com/auth/calendar",
    "drive_search": "https://www.googleapis.com/auth/drive.readonly",
    "drive_read": "https://www.googleapis.com/auth/drive.readonly",
    "tasks_list": "https://www.googleapis.com/auth/tasks.readonly",
    "tasks_create": "https://www.googleapis.com/auth/tasks",
    "tasks_modify": "https://www.googleapis.com/auth/tasks",
}

TOOL_DOMAIN: dict[str, str] = {
    "gmail_search": "gmail", "gmail_read": "gmail", "gmail_send": "gmail",
    "calendar_list": "calendar", "calendar_read": "calendar",
    "calendar_create": "calendar", "calendar_modify": "calendar",
    "drive_search": "drive", "drive_read": "drive",
    "tasks_list": "tasks", "tasks_create": "tasks", "tasks_modify": "tasks",
}

TOOL_ACTION: dict[str, str] = {
    "gmail_search": "SEARCH", "gmail_read": "READ", "gmail_send": "SEND",
    "calendar_list": "SEARCH", "calendar_read": "READ",
    "calendar_create": "CREATE", "calendar_modify": "MODIFY",
    "drive_search": "SEARCH", "drive_read": "READ",
    "tasks_list": "SEARCH", "tasks_create": "CREATE", "tasks_modify": "MODIFY",
}

# Google API base URLs per domain
GOOGLE_API_BASE: dict[str, str] = {
    "gmail": "https://gmail.googleapis.com/gmail/v1",
    "calendar": "https://www.googleapis.com/calendar/v3",
    "drive": "https://www.googleapis.com/drive/v3",
    "tasks": "https://tasks.googleapis.com/tasks/v1",
}


@dataclass
class OAuthState:
    """Persisted state for CSRF protection."""
    state: str
    delegation_id: str
    user_id: str
    created_at: float = field(default_factory=time.time)
    # optional: tenant_id for multi-tenant
    tenant_id: str = "default"


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    expires_in: int = 3600
    scope: str = ""
    token_type: str = "Bearer"
    id_token: str | None = None


# ---------------------------------------------------------------------------
# Vault interface (abstracted — works with EncryptedPostgresVault)
# ---------------------------------------------------------------------------
class VaultLike:
    """Minimal vault protocol — duck-typed for EncryptedPostgresVault."""
    async def store(self, user_id: str, provider: str, scope: str, token: bytes) -> str: ...
    async def retrieve(self, secret_ref: str, requester_agent_id: str) -> bytes: ...


# ---------------------------------------------------------------------------
# GoogleAdapter
# ---------------------------------------------------------------------------
class GoogleAdapter:
    """Google OAuth 2.0 + Gmail/Calendar/Drive/Tasks MCP adapter."""

    name = "google"
    provider = "google"

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
        vault: Any | None = None,
        delegation_service: Any | None = None,
    ) -> None:
        self.client_id = client_id or os.getenv("GOOGLE_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("GOOGLE_CLIENT_SECRET", "")
        self.redirect_uri = redirect_uri or os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/oauth/callback")
        self.vault = vault
        self.delegation_service = delegation_service
        # in-memory state store (CSRF) — production should use Redis
        self._states: dict[str, OAuthState] = {}
        # delegation_id -> secret_ref mapping
        self._binding: dict[str, str] = {}

    # ---- OAuth 2.0 authorization_code flow --------------------------------

    def authorize_url(
        self,
        delegation_id: str,
        user_id: str,
        scopes: list[str] | None = None,
        tenant_id: str = "default",
        state: str | None = None,
        access_type: str = "offline",
        prompt: str = "consent",
    ) -> tuple[str, str]:
        """Build Google consent URL.

        Returns (url, state) — caller must persist state for exchange.
        """
        st = state or uuid.uuid4().hex
        self._states[st] = OAuthState(state=st, delegation_id=delegation_id, user_id=user_id, tenant_id=tenant_id)

        # Resolve scopes: explicit or union of all minimal scopes
        if scopes is None:
            # default: readonly scopes only (least privilege)
            scopes = [
                GOOGLE_SCOPES["gmail_read"],
                GOOGLE_SCOPES["calendar_read"],
                GOOGLE_SCOPES["drive_read"],
                GOOGLE_SCOPES["tasks_list"],
            ]
            # dedupe
            scopes = sorted(set(scopes))

        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": st,
            "access_type": access_type,
            "prompt": prompt,
        }
        url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
        return url, st

    async def exchange_code(self, code: str, state: str) -> dict[str, Any]:
        """Exchange authorization code for tokens + Vault store.

        Validates state (CSRF), calls Google token endpoint, stores in Vault
        bound to delegation_id, and creates credential binding if
        delegation_service is available.
        """
        st = self._states.get(state)
        if st is None:
            raise ValueError(f"invalid or expired state: {state}")

        if httpx is None:
            raise RuntimeError("httpx not installed — pip install httpx")

        data = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data=data)
            resp.raise_for_status()
            tok = resp.json()

        access_token: str = tok["access_token"]
        refresh_token: str | None = tok.get("refresh_token")
        scope: str = tok.get("scope", "")
        expires_in: int = int(tok.get("expires_in", 3600))

        # Vault binding — store token encrypted bound to delegation_id
        secret_ref: str | None = None
        if self.vault is not None:
            # Store raw token bundle as bytes (access + refresh)
            bundle = f"{access_token}::{refresh_token or ''}".encode()
            # vault.store expects (user_id, provider, scope, token)
            secret_ref = await self.vault.store(st.user_id, self.provider, scope, bundle)
            self._binding[st.delegation_id] = secret_ref

            # Create delegation credential binding if service available
            if self.delegation_service is not None:
                try:
                    self.delegation_service.bind_credential(
                        delegation_id=st.delegation_id,
                        provider=self.provider,
                        secret_ref=secret_ref,
                        scope=scope,
                    )
                except Exception:
                    pass  # binding is best-effort here; vault already persisted

        # State single-use
        self._states.pop(state, None)

        return {
            "delegation_id": st.delegation_id,
            "user_id": st.user_id,
            "tenant_id": st.tenant_id,
            "scope": scope,
            "expires_in": expires_in,
            "secret_ref": secret_ref,
            "has_refresh_token": refresh_token is not None,
        }

    async def refresh(self, refresh_token: str) -> TokenSet:
        """Refresh access token using refresh_token."""
        if httpx is None:
            raise RuntimeError("httpx not installed")
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data=data)
            resp.raise_for_status()
            tok = resp.json()
        return TokenSet(
            access_token=tok["access_token"],
            refresh_token=tok.get("refresh_token") or refresh_token,
            expires_in=int(tok.get("expires_in", 3600)),
            scope=tok.get("scope", ""),
            token_type=tok.get("token_type", "Bearer"),
        )

    async def revoke(self, token: str) -> bool:
        """Revoke token at Google + clear Vault binding if known."""
        if httpx is None:
            raise RuntimeError("httpx not installed")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(GOOGLE_REVOKE_URL, data={"token": token})
            # Google returns 200 even if already revoked
            return resp.status_code == 200

        # Vault revoke is handled by caller via vault.revoke(secret_ref)
        # and delegation_service.revoke(delegation_id) cascade

    # ---- Vault helpers ----------------------------------------------------

    async def get_access_token(self, secret_ref: str, agent_id: str) -> str:
        """Retrieve and decrypt access_token from Vault for agent."""
        if self.vault is None:
            raise RuntimeError("vault not configured")
        raw = await self.vault.retrieve(secret_ref, agent_id)
        # bundle is "access::refresh"
        parts = raw.decode().split("::", 1)
        return parts[0]

    # ---- MCP tool/resource registry ---------------------------------------

    def required_scope(self, tool_name: str) -> str | None:
        return GOOGLE_SCOPES.get(tool_name)

    def tool_action(self, tool_name: str) -> str:
        return TOOL_ACTION.get(tool_name, "EXECUTE")

    def tool_domain(self, tool_name: str) -> str:
        return TOOL_DOMAIN.get(tool_name, "gmail")

    async def list_tools(self) -> list[str]:
        return list(GOOGLE_SCOPES.keys())

    async def list_resources(self) -> list[str]:
        return ["gmail/user/*", "calendar/user/*", "drive/user/*", "tasks/user/*"]

    def describe_tools(self) -> list[dict[str, Any]]:
        """Detailed tool descriptors for MCP discovery."""
        return [
            {
                "name": name,
                "domain": TOOL_DOMAIN[name],
                "action": TOOL_ACTION[name],
                "scope": scope,
                "resource_pattern": f"{TOOL_DOMAIN[name]}/user/*",
            }
            for name, scope in GOOGLE_SCOPES.items()
        ]

    # ---- Tool call skeleton (httpx + owner isolation) --------------------

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        agent_context: dict[str, Any] | Any,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        """Skeleton for Gmail/Calendar/Drive/Tasks API calls.

        - Validates owner isolation (resource owner == context user)
        - Requires access_token (resolved from Vault via get_access_token if None and vault bound)
        - Dispatches via httpx to googleapis with Bearer token.

        In production, this is called via ExecutionGateway proxy after
        capability / policy checks. Direct calls should pass agent_context.
        """
        # Owner isolation check (same logic as execution_gateway.connectors.google)
        ctx_user = agent_context.get("user_id") if isinstance(agent_context, dict) else getattr(agent_context, "user_id", None)
        delegation_id = agent_context.get("delegation_id") if isinstance(agent_context, dict) else getattr(agent_context, "delegation_id", None)
        resource: str = args.get("resource") or args.get("resource_uri") or ""

        if resource:
            # Extract owner from resource like gmail/user/kim/...
            parts = resource.split("/")
            owner = None
            if len(parts) >= 3 and parts[1] == "user":
                owner = f"employee:{parts[2]}"
                if owner != ctx_user:
                    raise PermissionError(f"owner mismatch: resource owner={owner} caller={ctx_user}")

        # Delegation binding trace — ensure token is bound to delegation
        # (verification happens at gateway; here we just propagate)
        _ = delegation_id  # traced

        # If no access_token provided, try Vault lookup via delegation binding
        if access_token is None and delegation_id and delegation_id in self._binding:
            # caller should provide agent_id for vault retrieve
            agent_id = agent_context.get("agent_id") if isinstance(agent_context, dict) else getattr(agent_context, "agent_id", None)
            if agent_id and self.vault is not None:
                try:
                    access_token = await self.get_access_token(self._binding[delegation_id], agent_id)
                except Exception:
                    pass

        if access_token is None:
            # Skeleton: return planned request instead of failing when no token in dev
            return {
                "tool": tool_name,
                "status": "requires_token",
                "resource": resource,
                "scope": self.required_scope(tool_name),
                "message": "no access_token — complete OAuth flow first",
                "planned_request": self._planned_request(tool_name, args),
            }

        # Real httpx dispatch (minimal skeleton)
        if httpx is None:
            raise RuntimeError("httpx not installed")

        domain = self.tool_domain(tool_name)
        base = GOOGLE_API_BASE.get(domain, "")
        headers = {"Authorization": f"Bearer {access_token}"}

        # Build request per tool (skeleton — returns planned request when offline)
        planned = self._planned_request(tool_name, args)
        # In production, execute:
        #   async with httpx.AsyncClient(timeout=20) as client:
        #       resp = await client.request(planned["method"], f"{base}{planned['path']}", headers=headers, params=planned.get("params"))
        # For skeleton, return planned + headers hint
        return {
            "tool": tool_name,
            "domain": domain,
            "action": self.tool_action(tool_name),
            "resource": resource,
            "scope": self.required_scope(tool_name),
            "delegation_id": delegation_id,
            "request": planned,
            "auth_header_present": bool(headers.get("Authorization")),
            "_note": "skeleton: wire httpx call to googleapis with Bearer token; enforce owner isolation already checked",
        }

    def _planned_request(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Return planned HTTP request for given tool (no network)."""
        mapping: dict[str, dict[str, Any]] = {
            "gmail_search": {"method": "GET", "path": "/users/me/messages", "params": {"q": args.get("q", ""), "maxResults": args.get("max_results", 10)}},
            "gmail_read": {"method": "GET", "path": f"/users/me/messages/{args.get('message_id', args.get('id', ''))}"},
            "gmail_send": {"method": "POST", "path": "/users/me/messages/send", "json": {"raw": args.get("raw", "")}},
            "calendar_list": {"method": "GET", "path": "/users/me/calendarList"},
            "calendar_read": {"method": "GET", "path": f"/calendars/{args.get('calendar_id', 'primary')}/events/{args.get('event_id', '')}"},
            "calendar_create": {"method": "POST", "path": f"/calendars/{args.get('calendar_id', 'primary')}/events", "json": args.get("event", {})},
            "calendar_modify": {"method": "PATCH", "path": f"/calendars/{args.get('calendar_id', 'primary')}/events/{args.get('event_id', '')}", "json": args.get("event", {})},
            "drive_search": {"method": "GET", "path": "", "params": {"q": args.get("q", ""), "pageSize": args.get("page_size", 10)}},
            "drive_read": {"method": "GET", "path": f"/files/{args.get('file_id', args.get('id', ''))}", "params": {"alt": "media"}},
            "tasks_list": {"method": "GET", "path": f"/lists/{args.get('tasklist', '@default')}/tasks"},
            "tasks_create": {"method": "POST", "path": f"/lists/{args.get('tasklist', '@default')}/tasks", "json": args.get("task", {})},
            "tasks_modify": {"method": "PATCH", "path": f"/lists/{args.get('tasklist', '@default')}/tasks/{args.get('task_id', '')}", "json": args.get("task", {})},
        }
        return mapping.get(tool_name, {"method": "GET", "path": "/", "params": args})

    # Backwards compat helper
    async def revoke_delegation(self, delegation_id: str, token: str | None = None) -> dict[str, Any]:
        """Revoke delegation: revoke token at Google + cascade via delegation_service."""
        result: dict[str, Any] = {"delegation_id": delegation_id}
        if token:
            try:
                ok = await self.revoke(token)
                result["google_revoked"] = ok
            except Exception as e:
                result["google_revoked"] = False
                result["error"] = str(e)
        if self.delegation_service is not None:
            try:
                self.delegation_service.revoke(delegation_id)
                result["delegation_revoked"] = True
            except Exception as e:
                result["delegation_revoked"] = False
                result["error"] = str(e)
        # clear local binding
        self._binding.pop(delegation_id, None)
        return result

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "tools": list(GOOGLE_SCOPES.keys()),
            "resources": ["gmail/user/*", "calendar/user/*", "drive/user/*", "tasks/user/*"],
            "scopes": GOOGLE_SCOPES,
            "oauth": {
                "auth_url": GOOGLE_AUTH_URL,
                "token_url": GOOGLE_TOKEN_URL,
                "revoke_url": GOOGLE_REVOKE_URL,
            },
        }
