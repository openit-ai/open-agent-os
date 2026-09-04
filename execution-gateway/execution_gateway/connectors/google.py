"""Google Personal Connector — Section 9-10

Gmail / Calendar / Drive / Tasks personal 자원 담당.

보안 원칙:
- personal resource는 owner만 접근 가능 (owner isolation)
- scope 최소 단위 요청 + scope validation
- delegation_id / credential_binding_id 바인딩 검증
- rate limit hook + audit logging
- Execution Gateway (MCP) 경유: search/read/write wrappers
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore

try:
    from ..normalize import parse_resource, is_personal_resource, extract_owner_user_id
except ImportError:
    from execution_gateway.normalize import parse_resource, is_personal_resource, extract_owner_user_id  # type: ignore

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Read-only tools allowed on the direct Google API path (separate OAuth module).
# Write tools never use the direct path — they stay on gateway proxy/fallback.
READONLY_TOOLS: frozenset[str] = frozenset({
    "gmail_search",
    "gmail_read",
    "calendar_list",
    "calendar_read",
    "drive_search",
    "drive_read",
    "tasks_list",
})

# Refresh skew: treat tokens expiring within this window as expired.
_REFRESH_SKEW_SEC = 60.0


def _is_production() -> bool:
    """Canonical production gate: env_gate.is_production() first, OAOS_ENV fallback."""
    try:
        from execution_gateway.env_gate import is_production as _p  # type: ignore
        return bool(_p())
    except Exception:
        pass
    try:
        from .env_gate import is_production as _p2  # type: ignore
        return bool(_p2())
    except Exception:
        pass
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        if os.getenv(k, "").strip().lower() in ("production", "prod"):
            return True
    return False


def _parse_token_bundle(raw: bytes) -> dict[str, Any]:
    """Parse a Vault token bundle.

    Supports the connector JSON bundle
    ({"access_token","refresh_token","expires_at","scope"}) and the legacy
    adapter bundle (b"<access>::<refresh>").
    Never logs or prints secret values — callers must only audit metadata.
    """
    try:
        text = raw.decode("utf-8")
    except Exception as e:
        raise ValueError("invalid token bundle encoding") from e
    s = text.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
        except Exception as e:
            raise ValueError("invalid token bundle JSON") from e
        access = str(obj.get("access_token") or "")
        refresh = obj.get("refresh_token") or None
        expires_at = obj.get("expires_at")
        try:
            expires_at_f = float(expires_at) if expires_at is not None else None
        except (TypeError, ValueError):
            expires_at_f = None
        return {
            "access_token": access,
            "refresh_token": str(refresh) if refresh else None,
            "expires_at": expires_at_f,
            "scope": str(obj.get("scope") or ""),
        }
    parts = text.split("::", 1)
    return {
        "access_token": parts[0],
        "refresh_token": parts[1] if len(parts) > 1 and parts[1] else None,
        "expires_at": None,
        "scope": "",
    }


def _encode_token_bundle(
    access_token: str,
    refresh_token: str | None,
    expires_at: float | None,
    scope: str = "",
) -> bytes:
    return json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "scope": scope,
    }).encode("utf-8")


def _bundle_expired(bundle: dict[str, Any], now: float | None = None) -> bool:
    exp = bundle.get("expires_at")
    if exp is None:
        return False
    try:
        return (now if now is not None else time.time()) >= (float(exp) - _REFRESH_SKEW_SEC)
    except (TypeError, ValueError):
        return False

# ── Google OAuth Scopes ───────────────────────────────────────────────
# 각 tool → 필요 scope 매핑 (최소 권한)

GOOGLE_SCOPES: dict[str, str] = {
    # Gmail
    "gmail_search": "https://www.googleapis.com/auth/gmail.readonly",
    "gmail_read": "https://www.googleapis.com/auth/gmail.readonly",
    "gmail_send": "https://www.googleapis.com/auth/gmail.send",
    # Calendar
    "calendar_list": "https://www.googleapis.com/auth/calendar.readonly",
    "calendar_read": "https://www.googleapis.com/auth/calendar.readonly",
    "calendar_create": "https://www.googleapis.com/auth/calendar",
    "calendar_modify": "https://www.googleapis.com/auth/calendar",
    # Drive
    "drive_search": "https://www.googleapis.com/auth/drive.readonly",
    "drive_read": "https://www.googleapis.com/auth/drive.readonly",
    # Tasks
    "tasks_list": "https://www.googleapis.com/auth/tasks.readonly",
    "tasks_create": "https://www.googleapis.com/auth/tasks",
    "tasks_modify": "https://www.googleapis.com/auth/tasks",
}

# tool → canonical domain
TOOL_DOMAIN: dict[str, str] = {
    "gmail_search": "gmail", "gmail_read": "gmail", "gmail_send": "gmail",
    "calendar_list": "calendar", "calendar_read": "calendar", "calendar_create": "calendar", "calendar_modify": "calendar",
    "drive_search": "drive", "drive_read": "drive",
    "tasks_list": "tasks", "tasks_create": "tasks", "tasks_modify": "tasks",
}

# tool → 기본 action
TOOL_ACTION: dict[str, str] = {
    "gmail_search": "SEARCH", "gmail_read": "READ", "gmail_send": "SEND",
    "calendar_list": "SEARCH", "calendar_read": "READ", "calendar_create": "CREATE", "calendar_modify": "MODIFY",
    "drive_search": "SEARCH", "drive_read": "READ",
    "tasks_list": "SEARCH", "tasks_create": "CREATE", "tasks_modify": "MODIFY",
}

# Google API base — for planned request generation
GOOGLE_API_BASE: dict[str, str] = {
    "gmail": "https://gmail.googleapis.com/gmail/v1",
    "calendar": "https://www.googleapis.com/calendar/v3",
    "drive": "https://www.googleapis.com/drive/v3",
    "tasks": "https://tasks.googleapis.com/tasks/v1",
}


@dataclass(frozen=True)
class OwnerCheckResult:
    allowed: bool
    reason: str
    owner_user_id: str | None = None


class GoogleConnector:
    """Google personal tools connector — owner isolation + scope + rate limit + audit, via ExecutionGateway."""

    name = "google"
    provider = "google"

    def __init__(
        self,
        rate_limit_per_sec: float = 10,
        burst: int = 20,
        audit_ledger: Any | None = None,
        vault: Any | None = None,
        credential_binding: dict[str, str] | None = None,
        credential_provider: Any | None = None,
        delegation_service: Any | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_url: str | None = None,
        http_client_factory: Any | None = None,
    ) -> None:
        self._rate_limiter: Any = None
        self._audit_ledger = audit_ledger
        self._audit_events: list[dict[str, Any]] = []
        # ── OAOS-owned OAuth credential resolver (injected, never duplicated) ──
        # vault: existing Vault (e.g. EncryptedPostgresVault) with
        #   async store(user_id, provider, scope, token_bytes) -> secret_ref
        #   async retrieve(secret_ref, requester_agent_id) -> bytes
        # credential_binding: delegation_id -> secret_ref map owned by the
        #   separate OAuth module (registered via bind_credential()).
        # credential_provider: optional dict-like ({delegation_id: secret_ref})
        #   or callable resolving secret_ref for (delegation_id, agent_context).
        #   May be sync or async. Never reads Hermes files.
        self._vault = vault
        self._credential_binding: dict[str, str] = dict(credential_binding or {})
        self._credential_provider = credential_provider
        self._delegation_service = delegation_service
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url or os.getenv("GOOGLE_TOKEN_URL", GOOGLE_TOKEN_URL)
        self._http_client_factory = http_client_factory
        try:
            from execution_gateway.tool_policy import ToolRateLimiter  # type: ignore
            self._rate_limiter = ToolRateLimiter(rate_per_sec=rate_limit_per_sec, burst=burst)
        except Exception:
            try:
                from tool_policy import ToolRateLimiter  # type: ignore
                self._rate_limiter = ToolRateLimiter(rate_per_sec=rate_limit_per_sec, burst=burst)
            except Exception:
                self._rate_limiter = None
        self._simple_buckets: dict[str, list[float]] = {}

    # ── Credential resolver wiring (separate OAuth module owns the flow) ──
    def set_vault(self, vault: Any) -> None:
        """Inject/replace the Vault at runtime (e.g. from gateway lifespan)."""
        self._vault = vault

    def bind_credential(self, delegation_id: str, secret_ref: str) -> None:
        """Register an owner-bound secret_ref for a delegation.

        Called by the separate OAuth module after it stores the encrypted
        token bundle via Vault. The connector never stores secrets itself.
        """
        if not delegation_id or not secret_ref:
            raise ValueError("delegation_id and secret_ref are required")
        self._credential_binding[delegation_id] = secret_ref

    def unbind_credential(self, delegation_id: str) -> None:
        self._credential_binding.pop(delegation_id, None)

    def _update_binding_ref(self, delegation_id: str | None, new_ref: str) -> None:
        """Best-effort write-back of a refreshed secret_ref to all binding stores."""
        if not delegation_id or not new_ref:
            return
        self._credential_binding[str(delegation_id)] = new_ref
        prov = self._credential_provider
        if isinstance(prov, dict):
            try:
                prov[str(delegation_id)] = new_ref
            except Exception:
                pass
        else:
            for meth in ("bind_credential", "set_binding", "update_binding"):
                try:
                    fn = getattr(prov, meth, None)
                except Exception:
                    fn = None
                if callable(fn):
                    try:
                        fn(str(delegation_id), new_ref)
                    except Exception:
                        continue
                    break

    def _ctx_dict(self, agent_context: dict | Any) -> dict[str, Any]:
        if isinstance(agent_context, dict):
            return agent_context
        out: dict[str, Any] = {}
        for k in ("user_id", "agent_id", "tenant_id", "delegation_id", "credential_binding_id", "granted_scope", "scope"):
            try:
                v = getattr(agent_context, k, None)
            except Exception:
                v = None
            if v is not None:
                out[k] = v
        return out

    def _requester_agent_id(self, ctx: dict[str, Any]) -> str | None:
        agent_id = ctx.get("agent_id")
        if agent_id:
            return str(agent_id)
        user_id = ctx.get("user_id")
        if user_id and ":" in str(user_id):
            return f"agent:assistant:{str(user_id).split(':', 1)[-1]}"
        return None

    def _binding_secret_ref(self, delegation_id: str) -> str | None:
        if delegation_id and delegation_id in self._credential_binding:
            return self._credential_binding[delegation_id]
        prov = self._credential_provider
        if prov is not None and delegation_id:
            try:
                if isinstance(prov, dict):
                    ref = prov.get(delegation_id)
                    if ref:
                        return str(ref)
                elif hasattr(prov, "get") and not callable(getattr(prov, "get", None)) is False:
                    try:
                        ref = prov.get(delegation_id)  # type: ignore[attr-defined]
                        if ref and not inspect.isawaitable(ref):
                            return str(ref)
                    except Exception:
                        pass
            except Exception:
                pass
        if self._delegation_service is not None and delegation_id:
            try:
                list_fn = getattr(self._delegation_service, "list_bindings_for_delegation", None)
                listed = list_fn(delegation_id) if callable(list_fn) else []
                bindings = list(listed) if isinstance(listed, (list, tuple, set)) else []
                if not bindings:
                    ids = getattr(self._delegation_service, "_delegation_bindings", {}).get(delegation_id, set())
                    bindings = [self._delegation_service.get_binding(bid) for bid in list(ids)]
                for b in bindings:
                    if b is None or getattr(b, "provider", "google") != "google":
                        continue
                    status = getattr(b, "status", "ACTIVE")
                    status_v = getattr(status, "value", status)
                    if str(status_v).upper() != "ACTIVE":
                        continue
                    ref = getattr(b, "secret_ref", None)
                    if ref:
                        return str(ref)
            except Exception:
                pass
        return None

    def resolve_secret_ref(
        self,
        agent_context: dict | Any,
        delegation_id: str | None = None,
    ) -> str | None:
        """Resolve the owner-bound secret_ref for this call (sync part).

        Order: credential_binding_id (via delegation_service/binding map) →
        delegation_id (binding map → provider dict → delegation_service).
        Returns None when unresolvable (caller fail-closes in production).
        """
        ctx = self._ctx_dict(agent_context)
        did = delegation_id or ctx.get("delegation_id")
        cbid = ctx.get("credential_binding_id")
        if cbid:
            if self._delegation_service is not None:
                try:
                    b = self._delegation_service.get_binding(cbid)
                    if b is not None:
                        status_v = getattr(getattr(b, "status", "ACTIVE"), "value", getattr(b, "status", "ACTIVE"))
                        if str(status_v).upper() == "ACTIVE" and getattr(b, "provider", "google") == "google":
                            if did is None or getattr(b, "delegation_id", did) == did:
                                ref = getattr(b, "secret_ref", None)
                                if ref:
                                    return str(ref)
                except Exception:
                    pass
            if cbid in self._credential_binding:
                return self._credential_binding[cbid]
        if did:
            ref = self._binding_secret_ref(str(did))
            if ref:
                return ref
        return None

    async def _aresolve_secret_ref(
        self,
        agent_context: dict | Any,
        delegation_id: str | None = None,
    ) -> str | None:
        ref = self.resolve_secret_ref(agent_context, delegation_id)
        if ref:
            return ref
        # async credential_provider support (sync path cannot await)
        ctx = self._ctx_dict(agent_context)
        did = delegation_id or ctx.get("delegation_id")
        prov = self._credential_provider
        if prov is not None and callable(prov) and did:
            for args in ((str(did), ctx), (ctx,), (str(did),)):
                try:
                    out = prov(*args)  # type: ignore[operator]
                except TypeError:
                    continue
                except Exception:
                    break
                try:
                    if inspect.isawaitable(out):
                        out = await out
                    if out:
                        return str(out) if not isinstance(out, dict) else str(out.get("secret_ref") or out.get("ref") or "")
                except Exception:
                    break
        return None

    def _oauth_client(self) -> tuple[str, str]:
        cid = self._client_id or os.getenv("GOOGLE_CLIENT_ID", "")
        csec = self._client_secret or os.getenv("GOOGLE_CLIENT_SECRET", "")
        return cid, csec

    def _http_factory(self) -> Any:
        if self._http_client_factory is not None:
            return self._http_client_factory
        if httpx is None:
            raise RuntimeError("httpx not installed — pip install httpx")
        return httpx.AsyncClient

    async def refresh_access_token(self, refresh_token: str, scope: str = "") -> dict[str, Any]:
        """Refresh an access token via the Google token endpoint.

        Client credentials come from constructor/env config only — never from
        Vault or request args. Returns dict with access_token/refresh_token/
        expires_at/scope. Never logs secret values.
        """
        if not refresh_token:
            raise ValueError("no refresh_token available")
        cid, csec = self._oauth_client()
        if not cid or not csec:
            raise RuntimeError("google oauth client not configured (GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET)")
        data = {
            "client_id": cid,
            "client_secret": csec,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        factory = self._http_factory()
        async with factory(timeout=15) as client:
            resp = await client.post(self._token_url, data=data)
            resp.raise_for_status()
            tok = resp.json()
        access = (tok.get("access_token") or "") if isinstance(tok, dict) else ""
        if not access:
            raise RuntimeError("token refresh failed: no access_token in response")
        try:
            expires_in = int(tok.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        return {
            "access_token": access,
            "refresh_token": tok.get("refresh_token") or refresh_token,
            "expires_at": time.time() + expires_in,
            "expires_in": expires_in,
            "scope": tok.get("scope", scope) if isinstance(tok, dict) else scope,
        }

    async def resolve_access_token(
        self,
        agent_context: dict | Any,
        delegation_id: str | None = None,
    ) -> tuple[str, str]:
        """Resolve a live access token via Vault + refresh.

        Returns (access_token, secret_ref). Refreshes via the Google token
        endpoint when the stored bundle is expired and persists the refreshed
        bundle only via Vault.store (binding map updated, old ref revoked
        best-effort). Owner isolation is enforced by Vault.retrieve.
        Raises LookupError (no binding), RuntimeError (no vault/token), or
        PermissionError (owner/revocation) — all fail-closed.
        """
        ctx = self._ctx_dict(agent_context)
        did = delegation_id or ctx.get("delegation_id")
        secret_ref = await self._aresolve_secret_ref(agent_context, did)
        if not secret_ref:
            raise LookupError("no credential binding for this delegation/context")
        if self._vault is None:
            raise RuntimeError("vault not configured")
        requester = self._requester_agent_id(ctx)
        if not requester:
            raise PermissionError("missing agent identity for credential retrieve")
        raw = await self._vault.retrieve(secret_ref, requester)
        bundle = _parse_token_bundle(raw)
        access = bundle.get("access_token") or ""
        if not access:
            raise RuntimeError("empty access token in vault bundle")
        if _bundle_expired(bundle):
            refresh_token = bundle.get("refresh_token")
            if not refresh_token:
                raise RuntimeError("access token expired and no refresh_token available")
            user_id = ctx.get("user_id")
            if not user_id:
                raise PermissionError("missing user_id for token refresh store")
            refreshed = await self.refresh_access_token(refresh_token, bundle.get("scope", ""))
            new_scope = refreshed.get("scope", bundle.get("scope", ""))
            new_bundle = _encode_token_bundle(
                refreshed["access_token"],
                refreshed.get("refresh_token") or refresh_token,
                refreshed.get("expires_at"),
                new_scope,
            )
            new_ref = await self._vault.store(str(user_id), self.provider, new_scope, new_bundle)
            if did:
                self._update_binding_ref(str(did), new_ref)
            if new_ref != secret_ref:
                try:
                    revoke = getattr(self._vault, "revoke", None)
                    if callable(revoke):
                        await revoke(secret_ref)  # type: ignore[misc]
                except Exception:
                    pass
            self._audit("OAUTH_TOKEN_REFRESH", {
                "delegation_id": did,
                "secret_ref": new_ref,
                "scope": new_scope,
            })
            return refreshed["access_token"], new_ref
        return access, secret_ref

    async def call_readonly_api(
        self,
        tool_name: str,
        args: dict[str, Any],
        access_token: str,
    ) -> dict[str, Any]:
        """Real read-only Google API GET with a Bearer token (no mock).

        Only tools in READONLY_TOOLS are allowed; write tools raise ValueError
        so they can never auto-send via this path.
        """
        if tool_name not in READONLY_TOOLS:
            raise ValueError(f"tool {tool_name} is not read-only — direct API path refused")
        if not access_token:
            raise RuntimeError("missing access token for google api call")
        if httpx is None and self._http_client_factory is None:
            raise RuntimeError("httpx not installed — pip install httpx")
        planned = self._planned(tool_name, args)
        domain = self.tool_domain(tool_name)
        base = GOOGLE_API_BASE.get(domain, "")
        path = str(planned.get("path", "/") or "/")
        params = planned.get("params")
        url = f"{base}{path}"
        headers = {"Authorization": f"Bearer {access_token}"}
        factory = self._http_factory()
        async with factory(timeout=15) as client:
            resp = await client.get(url, headers=headers, params=params)
            if getattr(resp, "status_code", 200) == 401:
                raise PermissionError("google api unauthorized (token rejected)")
            raise_for = getattr(resp, "raise_for_status", None)
            if callable(raise_for):
                raise_for()
            try:
                data = resp.json()
            except Exception:
                text = getattr(resp, "text", "") or ""
                data = {"text": text[:4000]}
        self._audit("GOOGLE_API_CALL", {"tool": tool_name, "domain": domain, "status_code": getattr(resp, "status_code", None)})
        return {
            "tool": tool_name,
            "domain": domain,
            "action": self.tool_action(tool_name),
            "scope": self.required_scope(tool_name),
            "data": data,
            "status_code": getattr(resp, "status_code", 200),
            "via": "google_api",
        }

    def _audit(self, event_type: str, details: dict[str, Any]) -> None:
        evt = {"event_type": event_type, "provider": self.provider, "timestamp": datetime.now(timezone.utc).isoformat(), **details}
        self._audit_events.append(evt)
        logger.info("google-connector audit %s %s", event_type, details)
        if self._audit_ledger is not None:
            try:
                if hasattr(self._audit_ledger, "append"):
                    self._audit_ledger.append(evt)  # type: ignore
                elif hasattr(self._audit_ledger, "record"):
                    self._audit_ledger.record(evt)  # type: ignore
            except Exception:
                pass

    def audit_events(self) -> list[dict[str, Any]]:
        return list(self._audit_events)

    # ── Scope helpers ─────────────────────────────────────────────
    def required_scope(self, tool_name: str) -> str | None:
        return GOOGLE_SCOPES.get(tool_name)

    def validate_scope(self, tool_name: str, granted_scope: str) -> tuple[bool, str]:
        required = GOOGLE_SCOPES.get(tool_name)
        if required is None:
            return False, f"unknown tool: {tool_name}"
        if not granted_scope:
            return False, f"no granted scope for {tool_name} requires {required}"
        granted_set = set(granted_scope.split())
        if required in granted_set:
            return True, "scope ok"
        return False, f"scope mismatch: {tool_name} requires {required}, granted {granted_scope}"

    def tool_action(self, tool_name: str) -> str:
        return TOOL_ACTION.get(tool_name, "EXECUTE")

    def tool_domain(self, tool_name: str) -> str:
        return TOOL_DOMAIN.get(tool_name, "gmail")

    def list_tools(self) -> list[str]:
        return list(GOOGLE_SCOPES.keys())

    def list_resources(self) -> list[str]:
        return ["gmail/user/*", "calendar/user/*", "drive/user/*", "tasks/user/*"]

    # ── Owner isolation ──────────────────────────────────────────────

    def check_owner(
        self,
        agent_context: dict | object,
        resource: str,
        tool_name: str | None = None,
    ) -> OwnerCheckResult:
        """Personal credential owner 검증.

        - resource가 personal이면, resource owner == AgentContext.user_id 이어야 함
        - 빈 resource인 경우 tool의 domain으로 추정
        """
        if isinstance(agent_context, dict):
            ctx_user = agent_context.get("user_id")
            ctx_agent = agent_context.get("agent_id")
        else:
            ctx_user = getattr(agent_context, "user_id", None)
            ctx_agent = getattr(agent_context, "agent_id", None)

        if not ctx_user:
            return OwnerCheckResult(False, "missing user_id in AgentContext")

        if not resource:
            return OwnerCheckResult(True, "no resource — deferred check", None)

        parsed = None
        try:
            parsed = parse_resource(resource)
        except ValueError as e:
            return OwnerCheckResult(False, f"invalid resource: {e}")

        if not parsed.is_personal:
            return OwnerCheckResult(True, "non-personal resource — no owner isolation", None)

        if parsed.domain not in ("gmail", "calendar", "drive", "tasks"):
            return OwnerCheckResult(True, f"domain {parsed.domain} not owned by google connector", None)

        owner = extract_owner_user_id(resource)
        if owner is None:
            return OwnerCheckResult(False, f"cannot extract owner from resource: {resource}")

        if owner != ctx_user:
            return OwnerCheckResult(
                False,
                f"owner mismatch: resource owner={owner} caller={ctx_user}",
                owner,
            )
        if ctx_agent:
            expected_agent = ctx_user.replace("employee:", "agent:assistant:", 1) if ctx_user.startswith("employee:") else None
            if expected_agent and ctx_agent != expected_agent:
                return OwnerCheckResult(False, f"agent mismatch: expected {expected_agent} got {ctx_agent}", owner)

        return OwnerCheckResult(True, "owner verified", owner)

    # ── Rate limit ────────────────────────────────────────────────
    def _rate_key(self, agent_context: dict | Any, tool_name: str) -> str:
        if isinstance(agent_context, dict):
            tenant = agent_context.get("tenant_id", "default")
            user = agent_context.get("user_id", "unknown")
        else:
            tenant = getattr(agent_context, "tenant_id", "default") or "default"
            user = getattr(agent_context, "user_id", "unknown") or "unknown"
        return f"{tenant}:{user}:{tool_name}"

    def check_rate_limit(self, agent_context: dict | Any, tool_name: str) -> tuple[bool, float]:
        key = self._rate_key(agent_context, tool_name)
        if self._rate_limiter is not None:
            try:
                allowed = self._rate_limiter.allow(key)
                if allowed:
                    return True, 0.0
                return False, self._rate_limiter.retry_after(key)
            except Exception:
                pass
        now = time.monotonic()
        bucket = self._simple_buckets.get(key, [])
        window = [t for t in bucket if now - t < 1.0]
        if len(window) >= 20:
            oldest = min(window) if window else now
            return False, max(0.0, 1.0 - (now - oldest))
        window.append(now)
        self._simple_buckets[key] = window
        return True, 0.0

    def validate_delegation(self, agent_context: dict | object, resource: str) -> tuple[bool, str]:
        """delegation_id / credential_binding_id 존재 여부 검증.

        Dev/test (no production env): permissive — returns ok when no binding
        is configured so the skeleton path keeps working.
        Production: fail-closed — a delegation_id/credential_binding_id with
        no resolvable owner-bound secret_ref is DENY.
        """
        ctx = self._ctx_dict(agent_context)  # type: ignore[arg-type]
        did = ctx.get("delegation_id")
        cbid = ctx.get("credential_binding_id")
        if not did and not cbid:
            return True, "ok"
        try:
            ref = self.resolve_secret_ref(agent_context)  # type: ignore[arg-type]
        except Exception:
            ref = None
        if ref:
            return True, "ok"
        if _is_production():
            return False, "no credential binding for delegation/context (fail-closed)"
        return True, "ok"

    def _credential_configured(self) -> bool:
        return (
            self._vault is not None
            or bool(self._credential_binding)
            or self._credential_provider is not None
            or self._delegation_service is not None
        )

    # ── Execution Gateway wrappers (search/read/write) ────────────
    def _enforce(self, tool_name: str, args: dict[str, Any], agent_context: dict | Any) -> str:
        """Common enforcement: owner + scope + rate limit + audit. Returns resolved resource."""
        resource: str = args.get("resource") or args.get("resource_uri") or ""
        if not resource:
            # synthesize from context
            ctx_user = agent_context.get("user_id") if isinstance(agent_context, dict) else getattr(agent_context, "user_id", "")
            if ctx_user:
                suffix = ctx_user.split(":", 1)[-1] if ":" in ctx_user else ctx_user
                resource = f"{self.tool_domain(tool_name)}/user/{suffix}"
        # owner
        res = self.check_owner(agent_context, resource, tool_name)
        if not res.allowed:
            self._audit("DENY_OWNER", {"tool": tool_name, "resource": resource, "reason": res.reason})
            raise PermissionError(res.reason)
        # scope if delegation scope present in context
        granted = ""
        if isinstance(agent_context, dict):
            granted = agent_context.get("granted_scope") or agent_context.get("scope") or ""
        else:
            granted = getattr(agent_context, "granted_scope", "") or getattr(agent_context, "scope", "") or ""
        if granted:
            ok, reason = self.validate_scope(tool_name, granted)
            if not ok:
                self._audit("DENY_SCOPE", {"tool": tool_name, "reason": reason})
                raise PermissionError(reason)
        # rate limit
        allowed, retry = self.check_rate_limit(agent_context, tool_name)
        if not allowed:
            self._audit("RATE_LIMITED", {"tool": tool_name, "retry_after": retry})
            raise RuntimeError(f"rate limited: {tool_name} retry_after={retry:.2f}s")
        self._audit("TOOL_CALL_ATTEMPT", {"tool": tool_name, "resource": resource})
        return resource

    def _planned(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
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

    async def call_via_gateway(self, tool_name: str, args: dict[str, Any], agent_context: dict[str, Any] | Any, capability_token: dict | str | None = None) -> dict[str, Any]:
        """Route tool call via Execution Gateway proxy (MCP). Includes all enforcement.

        Read-only tools with an injected Vault/binding take the direct Google
        API path (Bearer via httpx, refresh-on-expiry via Vault only). Write
        tools never take the direct path. When no credential is resolvable:
        production fails closed (PermissionError, no mock); dev/test keeps the
        existing gateway proxy/fallback skeleton behavior.
        """
        resource = self._enforce(tool_name, args, agent_context)
        planned = self._planned(tool_name, args)
        # ── Direct read-only Google API path (OAOS-owned OAuth) ──
        if tool_name in READONLY_TOOLS and self._credential_configured():
            try:
                access_token, secret_ref = await self.resolve_access_token(agent_context)
            except (LookupError, RuntimeError) as e:
                if _is_production():
                    self._audit("DENY_NO_CREDENTIAL", {"tool": tool_name, "resource": resource, "reason": str(e)})
                    raise PermissionError(f"no credential available for {tool_name}: {e}") from e
                access_token, secret_ref = "", ""
            except PermissionError:
                raise
            if access_token:
                try:
                    result = await self.call_readonly_api(tool_name, args, access_token)
                    result["resource"] = resource
                    return result
                except PermissionError as e:
                    # 401 from Google (token rejected) → single refresh retry when possible
                    if "unauthorized" in str(e).lower():
                        retried = await self._retry_api_after_refresh(tool_name, args, resource, agent_context, secret_ref)
                        if retried is not None:
                            return retried
                    raise
                except (ValueError, RuntimeError):
                    raise
                except Exception as e:
                    if _is_production():
                        self._audit("GOOGLE_API_ERROR", {"tool": tool_name, "resource": resource, "error": type(e).__name__})
                        raise RuntimeError(f"google api call failed: {type(e).__name__}") from e
                    logger.debug("google api direct path failed for %s, falling back: %s", tool_name, e)
            elif _is_production():
                self._audit("DENY_NO_CREDENTIAL", {"tool": tool_name, "resource": resource, "reason": "unresolvable credential"})
                raise PermissionError(f"no credential available for {tool_name} (fail-closed)")
        elif tool_name in READONLY_TOOLS and _is_production() and self._needs_credential(agent_context):
            self._audit("DENY_NO_CREDENTIAL", {"tool": tool_name, "resource": resource, "reason": "no vault/binding configured"})
            raise PermissionError(f"no credential available for {tool_name} (fail-closed)")
        # Try real gateway proxy if available
        try:
            from execution_gateway.proxy import proxy_tool_call  # type: ignore
            action = self.tool_action(tool_name)
            ctx: dict[str, Any] = {}
            if isinstance(agent_context, dict):
                ctx.update(agent_context)
            else:
                ctx.update({k: getattr(agent_context, k) for k in dir(agent_context) if not k.startswith("_")})
            ctx.setdefault("action", action)
            ctx.setdefault("resource", resource)
            result = await proxy_tool_call(tool_name, args, capability_token, ctx)
            self._audit("GATEWAY_PROXY", {"tool": tool_name, "resource": resource, "result_ok": result.get("ok")})
            return result
        except Exception as e:
            # fallback to planned request (mock) — dev/test only
            if _is_production():
                self._audit("DENY_GATEWAY_FALLBACK", {"tool": tool_name, "resource": resource})
                raise RuntimeError(f"gateway proxy unavailable in production for {tool_name} (fail-closed)") from e
            logger.debug("gateway proxy fallback for %s: %s", tool_name, e)
            self._audit("GATEWAY_FALLBACK", {"tool": tool_name, "resource": resource})
            return {"tool": tool_name, "resource": resource, "request": planned, "via": "fallback", "action": self.tool_action(tool_name), "scope": self.required_scope(tool_name)}

    def _needs_credential(self, agent_context: dict[str, Any] | Any) -> bool:
        """True when the context carries a delegation/binding that requires a credential."""
        ctx = self._ctx_dict(agent_context)
        return bool(ctx.get("delegation_id") or ctx.get("credential_binding_id"))

    async def _retry_api_after_refresh(
        self,
        tool_name: str,
        args: dict[str, Any],
        resource: str,
        agent_context: dict[str, Any] | Any,
        secret_ref: str,
    ) -> dict[str, Any] | None:
        """Single retry after a Google 401: force refresh from the stored bundle.

        Returns the API result on success, None when no refresh is possible
        (caller re-raises the original 401). Refreshed bundles persist only
        via Vault; secrets are never logged.
        """
        if self._vault is None or not secret_ref:
            return None
        ctx = self._ctx_dict(agent_context)
        requester = self._requester_agent_id(ctx)
        user_id = ctx.get("user_id")
        if not requester or not user_id:
            return None
        try:
            raw = await self._vault.retrieve(secret_ref, requester)
            bundle = _parse_token_bundle(raw)
        except Exception:
            return None
        refresh_token = bundle.get("refresh_token")
        if not refresh_token:
            return None
        try:
            refreshed = await self.refresh_access_token(refresh_token, bundle.get("scope", ""))
        except Exception:
            return None
        new_scope = refreshed.get("scope", bundle.get("scope", ""))
        try:
            new_ref = await self._vault.store(str(user_id), self.provider, new_scope, _encode_token_bundle(
                refreshed["access_token"],
                refreshed.get("refresh_token") or refresh_token,
                refreshed.get("expires_at"),
                new_scope,
            ))
        except Exception:
            return None
        did = ctx.get("delegation_id")
        if did:
            self._update_binding_ref(str(did), new_ref)
        try:
            result = await self.call_readonly_api(tool_name, args, refreshed["access_token"])
            result["resource"] = resource
            self._audit("OAUTH_TOKEN_REFRESH", {"delegation_id": did, "secret_ref": new_ref, "scope": new_scope})
            return result
        except Exception:
            return None

    # Convenience wrappers — each is a thin alias to call_via_gateway with typed name
    async def gmail_search(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, capability_token: Any | None = None) -> dict[str, Any]:
        return await self.call_via_gateway("gmail_search", args, agent_context, capability_token)

    async def gmail_read(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, capability_token: Any | None = None) -> dict[str, Any]:
        return await self.call_via_gateway("gmail_read", args, agent_context, capability_token)

    async def gmail_send(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, capability_token: Any | None = None) -> dict[str, Any]:
        return await self.call_via_gateway("gmail_send", args, agent_context, capability_token)

    async def calendar_list(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, capability_token: Any | None = None) -> dict[str, Any]:
        return await self.call_via_gateway("calendar_list", args, agent_context, capability_token)

    async def calendar_read(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, capability_token: Any | None = None) -> dict[str, Any]:
        return await self.call_via_gateway("calendar_read", args, agent_context, capability_token)

    async def calendar_create(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, capability_token: Any | None = None) -> dict[str, Any]:
        return await self.call_via_gateway("calendar_create", args, agent_context, capability_token)

    async def calendar_modify(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, capability_token: Any | None = None) -> dict[str, Any]:
        return await self.call_via_gateway("calendar_modify", args, agent_context, capability_token)

    async def drive_search(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, capability_token: Any | None = None) -> dict[str, Any]:
        return await self.call_via_gateway("drive_search", args, agent_context, capability_token)

    async def drive_read(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, capability_token: Any | None = None) -> dict[str, Any]:
        return await self.call_via_gateway("drive_read", args, agent_context, capability_token)

    async def tasks_list(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, capability_token: Any | None = None) -> dict[str, Any]:
        return await self.call_via_gateway("tasks_list", args, agent_context, capability_token)

    async def tasks_create(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, capability_token: Any | None = None) -> dict[str, Any]:
        return await self.call_via_gateway("tasks_create", args, agent_context, capability_token)

    async def tasks_modify(self, args: dict[str, Any], agent_context: dict[str, Any] | Any, capability_token: Any | None = None) -> dict[str, Any]:
        return await self.call_via_gateway("tasks_modify", args, agent_context, capability_token)

    def describe(self) -> dict:
        return {
            "name": self.name,
            "provider": self.provider,
            "tools": self.list_tools(),
            "resources": self.list_resources(),
            "scopes": GOOGLE_SCOPES,
        }
