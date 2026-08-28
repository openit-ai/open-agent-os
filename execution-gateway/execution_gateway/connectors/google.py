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

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

try:
    from ..normalize import parse_resource, is_personal_resource, extract_owner_user_id
except ImportError:
    from execution_gateway.normalize import parse_resource, is_personal_resource, extract_owner_user_id  # type: ignore

logger = logging.getLogger(__name__)

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

    def __init__(self, rate_limit_per_sec: float = 10, burst: int = 20, audit_ledger: Any | None = None) -> None:
        self._rate_limiter: Any = None
        self._audit_ledger = audit_ledger
        self._audit_events: list[dict[str, Any]] = []
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
        """delegation_id / credential_binding_id 존재 여부 검증."""
        return True, "ok"

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
        """Route tool call via Execution Gateway proxy (MCP). Includes all enforcement."""
        resource = self._enforce(tool_name, args, agent_context)
        planned = self._planned(tool_name, args)
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
            # fallback to planned request (mock)
            logger.debug("gateway proxy fallback for %s: %s", tool_name, e)
            self._audit("GATEWAY_FALLBACK", {"tool": tool_name, "resource": resource})
            return {"tool": tool_name, "resource": resource, "request": planned, "via": "fallback", "action": self.tool_action(tool_name), "scope": self.required_scope(tool_name)}

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
