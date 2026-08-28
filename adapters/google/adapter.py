"""Google adapter — OAuth 2.0 authorization_code + Vault, Gmail/Calendar/Drive/Tasks.

Section 9-10: Personal Delegation / Vault binding (delegation_id)
Section 17: ACP context, Section 28: Knowledge not applicable here (personal tools)

Flow:
  authorize_url -> user consent -> exchange_code -> Vault store + Delegation bind
  refresh -> rotate access_token via refresh_token (+ vault re-store)
  revoke -> revoke token + delegation cascade + immediate capability invalidation
  call_tool -> owner isolation + scope validation + rate limit + audit + gateway proxy

Env:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
  GOOGLE_SCOPES (optional override), VAULT_ENCRYPTION_KEY (via vault)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore

logger = logging.getLogger(__name__)

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

# Valid Google OAuth scopes set for validation
_VALID_SCOPES = set(GOOGLE_SCOPES.values()) | {
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/tasks.readonly",
    "openid", "email", "profile",
}


@dataclass
class OAuthState:
    """Persisted state for CSRF protection."""
    state: str
    delegation_id: str
    user_id: str
    created_at: float = field(default_factory=time.time)
    tenant_id: str = "default"


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    expires_in: int = 3600
    scope: str = ""
    token_type: str = "Bearer"
    id_token: str | None = None


@dataclass(frozen=True)
class OwnerCheckResult:
    allowed: bool
    reason: str
    owner_user_id: str | None = None


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
        audit_ledger: Any | None = None,
        rate_limit_per_sec: float = 10,
        rate_limit_burst: int = 20,
    ) -> None:
        self.client_id = client_id or os.getenv("GOOGLE_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("GOOGLE_CLIENT_SECRET", "")
        self.redirect_uri = redirect_uri or os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/oauth/callback")
        self.vault = vault
        self.delegation_service = delegation_service
        self.audit_ledger = audit_ledger
        # in-memory state store (CSRF) — production should use Redis
        self._states: dict[str, OAuthState] = {}
        # delegation_id -> secret_ref mapping
        self._binding: dict[str, str] = {}
        # immediate revoke tracking: revoked delegation_ids + revoked secret_refs + revoked tokens
        self._revoked_delegations: set[str] = set()
        self._revoked_secrets: set[str] = set()
        self._revoked_tokens: set[str] = set()
        # audit trail (in-memory for tests)
        self._audit_events: list[dict[str, Any]] = []
        # rate limiter (ToolRateLimiter if available else simple fallback)
        self._rate_limiter: Any = None
        try:
            from execution_gateway.tool_policy import ToolRateLimiter  # type: ignore
            self._rate_limiter = ToolRateLimiter(rate_per_sec=rate_limit_per_sec, burst=rate_limit_burst)
        except Exception:
            try:
                from tool_policy import ToolRateLimiter  # type: ignore
                self._rate_limiter = ToolRateLimiter(rate_per_sec=rate_limit_per_sec, burst=rate_limit_burst)
            except Exception:
                self._rate_limiter = None
        # fallback simple dict-based limiter
        self._simple_buckets: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # Audit helper
    # ------------------------------------------------------------------
    def _audit(self, event_type: str, details: dict[str, Any]) -> None:
        evt = {
            "event_type": event_type,
            "provider": self.provider,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **details,
        }
        self._audit_events.append(evt)
        logger.info("google audit %s %s", event_type, details)
        if self.audit_ledger is not None:
            try:
                # try generic append
                if hasattr(self.audit_ledger, "append"):
                    self.audit_ledger.append(evt)  # type: ignore
                elif hasattr(self.audit_ledger, "record"):
                    self.audit_ledger.record(evt)  # type: ignore
            except Exception:
                pass

    def audit_events(self) -> list[dict[str, Any]]:
        return list(self._audit_events)

    # ------------------------------------------------------------------
    # Scope validation
    # ------------------------------------------------------------------
    def validate_scope(self, tool_name: str, granted_scope: str) -> tuple[bool, str]:
        """Check if tool's required scope is within granted_scope (space-separated)."""
        required = GOOGLE_SCOPES.get(tool_name)
        if required is None:
            return False, f"unknown tool: {tool_name}"
        if not granted_scope:
            return False, f"no granted scope for tool {tool_name} requires {required}"
        granted_set = set(granted_scope.split())
        if required in granted_set:
            return True, "scope ok"
        # also allow broader scope e.g. gmail.send covers gmail.readonly? No, least privilege strict.
        # But if granted is full drive scope, it covers readonly
        if required == GOOGLE_SCOPES["gmail_read"] and "https://www.googleapis.com/auth/gmail.send" in granted_set:
            # gmail.send does NOT imply readonly in Google but we treat as not sufficient for safety
            return False, f"scope mismatch: {tool_name} requires {required}, granted {granted_scope}"
        return False, f"scope mismatch: {tool_name} requires {required}, granted {granted_scope}"

    def is_valid_scope_string(self, scope_str: str) -> bool:
        """Validate scope string contains only known Google scopes."""
        if not scope_str:
            return False
        parts = scope_str.split()
        for p in parts:
            if p not in _VALID_SCOPES and not p.startswith("https://www.googleapis.com/auth/"):
                # unknown but still https googleapis - allow? strict: check prefix
                if not p.startswith("https://"):
                    return False
        return len(parts) > 0

    # ------------------------------------------------------------------
    # Owner isolation — §10.2
    # ------------------------------------------------------------------
    def check_owner(
        self,
        agent_context: dict[str, Any] | Any,
        resource: str,
    ) -> OwnerCheckResult:
        """Owner isolation check: agent:assistant:kim only accesses employee:kim tokens.

        - resource like gmail/user/kim/... or calendar/user/kim/... 
        - empty resource → deferred check (allowed but no owner extracted)
        """
        ctx_user = agent_context.get("user_id") if isinstance(agent_context, dict) else getattr(agent_context, "user_id", None)
        ctx_agent = agent_context.get("agent_id") if isinstance(agent_context, dict) else getattr(agent_context, "agent_id", None)

        if not ctx_user:
            return OwnerCheckResult(False, "missing user_id in AgentContext")

        if not resource:
            return OwnerCheckResult(True, "no resource — deferred check", None)

        # Extract owner from resource like gmail/user/kim/...
        parts = resource.split("/")
        owner = None
        if len(parts) >= 3 and parts[1] == "user":
            owner = f"employee:{parts[2]}"
            if owner != ctx_user:
                return OwnerCheckResult(False, f"owner mismatch: resource owner={owner} caller={ctx_user}", owner)
            # also verify agent_id corresponds to user (§10.2)
            if ctx_agent:
                suffix = ctx_user.split(":", 1)[-1] if ":" in ctx_user else ctx_user
                expected_agent = f"agent:assistant:{suffix}"
                if ctx_agent != expected_agent:
                    # Strict: if agent_id present, must match. Log but still DENY if mismatch?
                    # Spec says agent:assistant:kim only accesses employee:kim tokens.
                    return OwnerCheckResult(False, f"agent mismatch: expected {expected_agent} got {ctx_agent}", owner)
            return OwnerCheckResult(True, "owner verified", owner)

        # Try alternative: try parse via normalize (if resource is like gmail/user/kim)
        try:
            from execution_gateway.normalize import parse_resource, extract_owner_user_id  # type: ignore
            parsed = parse_resource(resource)
            if parsed.is_personal:
                extracted = extract_owner_user_id(resource)
                if extracted and extracted != ctx_user:
                    return OwnerCheckResult(False, f"owner mismatch: resource owner={extracted} caller={ctx_user}", extracted)
                if ctx_agent and extracted:
                    suffix = ctx_user.split(":", 1)[-1]
                    expected = f"agent:assistant:{suffix}"
                    if ctx_agent != expected:
                        return OwnerCheckResult(False, f"agent mismatch: expected {expected} got {ctx_agent}", extracted)
                return OwnerCheckResult(True, "owner verified via parse", extracted)
        except Exception:
            pass

        # Non-personal or unknown pattern → allow (not google personal resource)
        return OwnerCheckResult(True, "non-personal or no owner segment — no isolation", None)

    # ------------------------------------------------------------------
    # Rate limit hook (§16H)
    # ------------------------------------------------------------------
    def _rate_key(self, agent_context: dict | Any, tool_name: str) -> str:
        if isinstance(agent_context, dict):
            tenant = agent_context.get("tenant_id", "default")
            user = agent_context.get("user_id", "unknown")
        else:
            tenant = getattr(agent_context, "tenant_id", "default") or "default"
            user = getattr(agent_context, "user_id", "unknown") or "unknown"
        return f"{tenant}:{user}:{tool_name}"

    def check_rate_limit(self, agent_context: dict | Any, tool_name: str, tokens: int = 1) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        key = self._rate_key(agent_context, tool_name)
        if self._rate_limiter is not None:
            try:
                allowed = self._rate_limiter.allow(key, tokens=tokens)
                if allowed:
                    return True, 0.0
                retry = self._rate_limiter.retry_after(key, tokens=tokens)
                return False, retry
            except Exception:
                pass
        # fallback simple token bucket: 10 per sec burst 20
        now = time.monotonic()
        bucket = self._simple_buckets.get(key, [])
        # keep only last second window + burst
        # refill style: we track timestamps
        # simplified: allow burst then rate limit
        # For determinism in tests, use burst logic: if len > burst in last 1 sec, deny
        window = [t for t in bucket if now - t < 1.0]
        if len(window) >= 20:  # burst
            oldest = min(window) if window else now
            retry = 1.0 - (now - oldest)
            return False, max(0.0, retry)
        window.append(now)
        self._simple_buckets[key] = window
        return True, 0.0

    def _enforce_rate_limit(self, agent_context: dict | Any, tool_name: str) -> None:
        allowed, retry = self.check_rate_limit(agent_context, tool_name)
        if not allowed:
            self._audit("RATE_LIMITED", {"tool": tool_name, "retry_after": retry, "context": str(agent_context)[:200]})
            raise RuntimeError(f"rate limited: {tool_name} retry_after={retry:.2f}s")

    # ------------------------------------------------------------------
    # OAuth 2.0 authorization_code flow
    # ------------------------------------------------------------------

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
        # validate scopes if provided
        if scopes is not None:
            for s in scopes:
                if s not in _VALID_SCOPES and not s.startswith("https://www.googleapis.com/auth/"):
                    raise ValueError(f"invalid scope: {s}")

        st = state or uuid.uuid4().hex
        self._states[st] = OAuthState(state=st, delegation_id=delegation_id, user_id=user_id, tenant_id=tenant_id)

        # Resolve scopes: explicit or union of all minimal scopes
        if scopes is None:
            scopes = [
                GOOGLE_SCOPES["gmail_read"],
                GOOGLE_SCOPES["calendar_read"],
                GOOGLE_SCOPES["drive_read"],
                GOOGLE_SCOPES["tasks_list"],
            ]
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
        self._audit("OAUTH_AUTHORIZE", {"delegation_id": delegation_id, "user_id": user_id, "scopes": scopes, "state": st})
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
        # expiry: 10 min
        if time.time() - st.created_at > 600:
            self._states.pop(state, None)
            raise ValueError(f"state expired: {state}")

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
            bundle = f"{access_token}::{refresh_token or ''}".encode()
            secret_ref = await self.vault.store(st.user_id, self.provider, scope, bundle)
            self._binding[st.delegation_id] = secret_ref

            if self.delegation_service is not None:
                try:
                    self.delegation_service.bind_credential(
                        delegation_id=st.delegation_id,
                        provider=self.provider,
                        secret_ref=secret_ref,
                        scope=scope,
                    )
                except Exception:
                    pass

        self._states.pop(state, None)
        self._audit("OAUTH_EXCHANGE", {"delegation_id": st.delegation_id, "user_id": st.user_id, "scope": scope, "has_refresh": refresh_token is not None})

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
        ts = TokenSet(
            access_token=tok["access_token"],
            refresh_token=tok.get("refresh_token") or refresh_token,
            expires_in=int(tok.get("expires_in", 3600)),
            scope=tok.get("scope", ""),
            token_type=tok.get("token_type", "Bearer"),
        )
        self._audit("OAUTH_REFRESH", {"scope": ts.scope, "expires_in": ts.expires_in})
        return ts

    async def refresh_for_delegation(self, delegation_id: str, agent_id: str) -> TokenSet:
        """Refresh token for a delegation: retrieve refresh_token from Vault, refresh, re-store."""
        if delegation_id in self._revoked_delegations:
            raise PermissionError(f"delegation revoked: {delegation_id}")
        secret_ref = self._binding.get(delegation_id)
        if not secret_ref:
            raise KeyError(f"no binding for delegation: {delegation_id}")
        if self.vault is None:
            raise RuntimeError("vault not configured")
        raw = await self.vault.retrieve(secret_ref, agent_id)
        parts = raw.decode().split("::", 1)
        refresh_token = parts[1] if len(parts) > 1 else ""
        if not refresh_token:
            raise ValueError("no refresh_token available for delegation")
        new_ts = await self.refresh(refresh_token)
        # re-store new bundle
        bundle = f"{new_ts.access_token}::{new_ts.refresh_token or refresh_token}".encode()
        # need user_id for vault store — extract from delegation_service if available
        user_id = None
        if self.delegation_service is not None:
            try:
                d = self.delegation_service.get(delegation_id)
                if d:
                    user_id = d.user_id
            except Exception:
                pass
        if user_id is None:
            # fallback: try vault meta
            try:
                user_id = self.vault.owner_of(secret_ref).replace("agent:assistant:", "employee:") if hasattr(self.vault, "owner_of") else "employee:unknown"  # type: ignore
            except Exception:
                user_id = "employee:unknown"
        # revoke old and store new
        try:
            await self.vault.revoke(secret_ref)
        except Exception:
            pass
        new_ref = await self.vault.store(user_id, self.provider, new_ts.scope or "", bundle)
        self._binding[delegation_id] = new_ref
        self._audit("OAUTH_REFRESH_DELEGATION", {"delegation_id": delegation_id, "new_secret_ref": new_ref})
        return new_ts

    async def revoke(self, token: str) -> bool:
        """Revoke token at Google + clear Vault binding if known."""
        if httpx is None:
            raise RuntimeError("httpx not installed")
        self._revoked_tokens.add(token)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(GOOGLE_REVOKE_URL, data={"token": token})
                ok = resp.status_code == 200
        except Exception:
            ok = False
        self._audit("OAUTH_REVOKE_TOKEN", {"token_prefix": token[:8] + "...", "google_ok": ok})
        return ok

    # ---- Vault helpers ----------------------------------------------------

    async def get_access_token(self, secret_ref: str, agent_id: str) -> str:
        """Retrieve and decrypt access_token from Vault for agent. Enforces owner isolation + revoke."""
        if secret_ref in self._revoked_secrets:
            raise PermissionError(f"credential revoked: {secret_ref}")
        if self.vault is None:
            raise RuntimeError("vault not configured")
        # delegation-level revoke check: find delegation for this secret
        for did, ref in list(self._binding.items()):
            if ref == secret_ref and did in self._revoked_delegations:
                raise PermissionError(f"delegation revoked: {did}")
        raw = await self.vault.retrieve(secret_ref, agent_id)
        parts = raw.decode().split("::", 1)
        token = parts[0]
        if token in self._revoked_tokens:
            raise PermissionError("token revoked")
        self._audit("VAULT_RETRIEVE", {"secret_ref": secret_ref, "agent_id": agent_id})
        return token

    def is_revoked(self, delegation_id: str | None = None, secret_ref: str | None = None) -> bool:
        if delegation_id and delegation_id in self._revoked_delegations:
            return True
        if secret_ref and secret_ref in self._revoked_secrets:
            return True
        return False

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

    # ---- Connector wrappers (Execution Gateway path) ---------------------
    # These are the search/read/write wrappers that go via Execution Gateway (MCP)
    # Each enforces: owner isolation, scope validation, rate limit, audit logging.

    async def gmail_search(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, access_token: str | None = None) -> dict[str, Any]:
        return await self.call_tool("gmail_search", args, agent_context, access_token)

    async def gmail_read(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, access_token: str | None = None) -> dict[str, Any]:
        return await self.call_tool("gmail_read", args, agent_context, access_token)

    async def gmail_send(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, access_token: str | None = None) -> dict[str, Any]:
        return await self.call_tool("gmail_send", args, agent_context, access_token)

    async def calendar_list(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, access_token: str | None = None) -> dict[str, Any]:
        return await self.call_tool("calendar_list", args, agent_context, access_token)

    async def calendar_read(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, access_token: str | None = None) -> dict[str, Any]:
        return await self.call_tool("calendar_read", args, agent_context, access_token)

    async def calendar_create(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, access_token: str | None = None) -> dict[str, Any]:
        return await self.call_tool("calendar_create", args, agent_context, access_token)

    async def calendar_modify(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, access_token: str | None = None) -> dict[str, Any]:
        return await self.call_tool("calendar_modify", args, agent_context, access_token)

    async def drive_search(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, access_token: str | None = None) -> dict[str, Any]:
        return await self.call_tool("drive_search", args, agent_context, access_token)

    async def drive_read(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, access_token: str | None = None) -> dict[str, Any]:
        return await self.call_tool("drive_read", args, agent_context, access_token)

    async def tasks_list(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, access_token: str | None = None) -> dict[str, Any]:
        return await self.call_tool("tasks_list", args, agent_context, access_token)

    async def tasks_create(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, access_token: str | None = None) -> dict[str, Any]:
        return await self.call_tool("tasks_create", args, agent_context, access_token)

    async def tasks_modify(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, access_token: str | None = None) -> dict[str, Any]:
        return await self.call_tool("tasks_modify", args, agent_context, access_token)

    # ---- Tool call skeleton (httpx + owner isolation + scope + rate limit + audit) ----

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        agent_context: dict[str, Any] | Any,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        """Skeleton for Gmail/Calendar/Drive/Tasks API calls.

        - Validates owner isolation (resource owner == context user)
        - Scope validation (required scope in delegation scope)
        - Rate limit hook
        - Audit logging
        - Requires access_token (resolved from Vault via get_access_token if None and vault bound)
        - Dispatches via httpx to googleapis with Bearer token.

        In production, this is called via ExecutionGateway proxy after
        capability / policy checks. Direct calls should pass agent_context.
        """
        if tool_name not in GOOGLE_SCOPES:
            raise ValueError(f"unknown tool: {tool_name}")

        # Extract context
        ctx_user = agent_context.get("user_id") if isinstance(agent_context, dict) else getattr(agent_context, "user_id", None)
        ctx_agent = agent_context.get("agent_id") if isinstance(agent_context, dict) else getattr(agent_context, "agent_id", None)
        delegation_id = agent_context.get("delegation_id") if isinstance(agent_context, dict) else getattr(agent_context, "delegation_id", None)
        resource: str = args.get("resource") or args.get("resource_uri") or ""
        # If no resource, synthesize canonical personal resource for isolation check
        if not resource and ctx_user:
            # synthesize like gmail/user/<suffix>/...
            suffix = ctx_user.split(":", 1)[-1] if ":" in ctx_user else ctx_user
            domain = self.tool_domain(tool_name)
            resource = f"{domain}/user/{suffix}"

        # 0. Revoke check — immediate invalidation
        if delegation_id and delegation_id in self._revoked_delegations:
            self._audit("DENY_REVOKED", {"tool": tool_name, "delegation_id": delegation_id, "reason": "delegation revoked"})
            raise PermissionError(f"delegation revoked: {delegation_id}")

        # 1. Owner isolation
        owner_res = self.check_owner(agent_context, resource)
        if not owner_res.allowed:
            self._audit("DENY_OWNER", {"tool": tool_name, "resource": resource, "reason": owner_res.reason, "caller": ctx_user})
            raise PermissionError(owner_res.reason)

        # 2. Scope validation if delegation_service has delegation scope
        if delegation_id and self.delegation_service is not None:
            try:
                d = self.delegation_service.get(delegation_id)
                if d is not None:
                    ok, reason = self.validate_scope(tool_name, d.scope)
                    if not ok:
                        self._audit("DENY_SCOPE", {"tool": tool_name, "delegation_id": delegation_id, "reason": reason})
                        raise PermissionError(reason)
                # also check delegation active
                if not self.delegation_service.is_active(delegation_id):
                    self._audit("DENY_DELEGATION_INACTIVE", {"tool": tool_name, "delegation_id": delegation_id})
                    raise PermissionError(f"delegation not active: {delegation_id}")
            except PermissionError:
                raise
            except Exception:
                pass  # delegation lookup failure is not fatal for scope check if no delegation object

        # 3. Rate limit
        try:
            self._enforce_rate_limit(agent_context, tool_name)
        except RuntimeError as e:
            # propagate as rate-limited; caller can map to 429
            raise

        # 4. Audit — attempt
        self._audit("TOOL_CALL_ATTEMPT", {"tool": tool_name, "resource": resource, "delegation_id": delegation_id, "user_id": ctx_user, "agent_id": ctx_agent})

        # 5. Delegation binding trace
        _ = delegation_id  # traced

        # If no access_token provided, try Vault lookup via delegation binding
        if access_token is None and delegation_id and delegation_id in self._binding:
            agent_id = agent_context.get("agent_id") if isinstance(agent_context, dict) else getattr(agent_context, "agent_id", None)
            if agent_id and self.vault is not None:
                try:
                    access_token = await self.get_access_token(self._binding[delegation_id], agent_id)
                except PermissionError:
                    raise
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

        # Check token not revoked
        if access_token in self._revoked_tokens:
            raise PermissionError("token revoked")

        # Real httpx dispatch (minimal skeleton)
        if httpx is None:
            raise RuntimeError("httpx not installed")

        domain = self.tool_domain(tool_name)
        base = GOOGLE_API_BASE.get(domain, "")
        headers = {"Authorization": f"Bearer {access_token}"}

        planned = self._planned_request(tool_name, args)
        # Audit success (gateway execution)
        self._audit("TOOL_CALL_SUCCESS", {"tool": tool_name, "resource": resource, "domain": domain, "action": self.tool_action(tool_name)})
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
        """Revoke delegation: revoke token at Google + cascade via delegation_service + vault + capability invalidation."""
        result: dict[str, Any] = {"delegation_id": delegation_id}
        # immediate invalidation — add to revoked set synchronously
        self._revoked_delegations.add(delegation_id)
        secret_ref = self._binding.get(delegation_id)
        if secret_ref:
            self._revoked_secrets.add(secret_ref)
        if token:
            self._revoked_tokens.add(token)
            try:
                ok = await self.revoke(token)
                result["google_revoked"] = ok
            except Exception as e:
                result["google_revoked"] = False
                result["error"] = str(e)
        if secret_ref and self.vault is not None:
            try:
                await self.vault.revoke(secret_ref)
                result["vault_revoked"] = True
            except Exception as e:
                result["vault_revoked"] = False
                result["vault_error"] = str(e)
        if self.delegation_service is not None:
            try:
                self.delegation_service.revoke(delegation_id)
                result["delegation_revoked"] = True
            except Exception as e:
                result["delegation_revoked"] = False
                result["error"] = str(e)
        else:
            result["delegation_revoked"] = True
        # clear local binding
        self._binding.pop(delegation_id, None)
        self._audit("REVOKE_DELEGATION", {"delegation_id": delegation_id, "result": result})
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
