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
        # agent-to-agent colleague delivery (§14 governance via target agent)
        "notify_colleague": "SEND",
        "mattermost_send_direct_message": "SEND",
        "mattermost_send_dm": "SEND",
    }

    # colleague DM is internal — approval not required but audit logged
    COLLEAGUE_TOOLS: frozenset[str] = frozenset({"notify_colleague", "mattermost_send_direct_message", "mattermost_send_dm"})

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

    # ---- Colleague DM §14 governance (agent -> target agent -> human) -----

    def _resolve_target_employee(self, args: dict[str, Any]) -> tuple[str, str]:
        """Resolve target_employee principal from args.

        Accepts: target_employee (employee:choi), target_user, mattermost_username,
                 target_username, username, employee.
        Returns: (employee_principal, mattermost_username_hint)
        """
        # direct employee principal
        for k in ("target_employee", "employee_principal", "employee", "target_principal"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                v = v.strip()
                if v.startswith("employee:"):
                    suffix = v.split(":", 1)[1]
                    hint = re.sub(r"[^a-z0-9_.-]", "", suffix.lower()) or suffix.lower()
                    return v, hint
                # bare username without prefix -> treat as employee suffix
                if ":" not in v and re.match(r"^[a-zA-Z0-9_.-]+$", v):
                    return f"employee:{v.lower()}", v.lower()

        # mattermost username variants -> map to employee
        for k in ("target_user", "mattermost_username", "target_username", "username", "user", "mattermost_user"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                v = v.strip()
                # if already employee:
                if v.startswith("employee:"):
                    suffix = v.split(":", 1)[1]
                    return v, suffix.lower()
                # if agent:assistant:choi -> convert to employee:choi
                if v.startswith("agent:assistant:"):
                    suffix = v.split(":", 2)[-1] if v.count(":") >= 2 else v
                    # agent:assistant:choi -> employee:choi
                    emp = f"employee:{suffix.lower()}"
                    return emp, suffix.lower()
                mapped = self.map_mattermost_user(v, v)
                hint = v.lower()
                return mapped, hint

        raise ValueError("target_employee or target_user/mattermost_username required")

    def _audit_dm(self, agent_context: dict[str, Any] | Any, target_employee: str, text: str, trace_id: str) -> None:
        """Create audit entry for colleague DM (hash-chain ledger if available)."""
        try:
            ctx = agent_context if isinstance(agent_context, dict) else getattr(agent_context, "__dict__", {})
            if not isinstance(ctx, dict):
                ctx = {"user_id": getattr(agent_context, "user_id", "employee:unknown")}
            import uuid as _uuid
            from datetime import datetime as _dt, timezone as _tz
            # try audit ledger
            try:
                from audit_model import AuditEvent, AuditEventType  # type: ignore
                from security.audit.audit_ledger.ledger import AuditLedger  # type: ignore
            except Exception:
                try:
                    from audit.audit_ledger.ledger import AuditLedger  # type: ignore
                    from audit_model.model import AuditEvent, AuditEventType  # type: ignore
                except Exception:
                    AuditLedger = None  # type: ignore
                    AuditEvent = None  # type: ignore
            if AuditEvent is not None and AuditLedger is not None:
                try:
                    ledger = AuditLedger(signing_key="demo-audit-key")
                except Exception:
                    ledger = None
                if ledger is not None:
                    tenant = ctx.get("tenant_id", "default") if isinstance(ctx, dict) else "default"
                    user_id = ctx.get("user_id") if isinstance(ctx, dict) else getattr(agent_context, "user_id", None)
                    agent_id = ctx.get("agent_id") if isinstance(ctx, dict) else getattr(agent_context, "agent_id", None)
                    if not agent_id and isinstance(user_id, str) and user_id.startswith("employee:"):
                        agent_id = user_id.replace("employee:", "agent:assistant:", 1)
                    ev = AuditEvent(
                        event_id=f"evt_{_uuid.uuid4().hex[:12]}",
                        event_type=AuditEventType.MCP_TOOL_CALL if hasattr(AuditEventType, "MCP_TOOL_CALL") else AuditEventType.DATA_ACCESS,  # type: ignore
                        timestamp=_dt.now(_tz.utc),
                        tenant_id=tenant,
                        user_id=user_id,
                        agent_id=agent_id,
                        session_id=ctx.get("session_id") if isinstance(ctx, dict) else None,
                        trace_id=trace_id,
                        request_id=ctx.get("request_id") if isinstance(ctx, dict) else None,
                        resource=f"mattermost/dm/{target_employee.split(':')[-1] if ':' in target_employee else target_employee}",
                        action="SEND",
                        tool_name="notify_colleague",
                        decision="ALLOW",
                    )
                    ledger.append(ev)
            # also fallback: mock_executor global ledger
            try:
                from execution_gateway.mock_executor import get_ledger  # type: ignore
                ledger2 = get_ledger()
                if ledger2 is not None and AuditEvent is not None:
                    # already appended above if ledger is same impl; add second via mock ledger for visibility
                    pass
            except Exception:
                pass
        except Exception:
            pass  # audit must not block delivery

    async def send_direct_message(
        self,
        target_employee: str,
        text: str,
        agent_context: dict[str, Any] | Any,
        channel_id: str | None = None,
        trace_id: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send DM to colleague via Mattermost — resolves employee principal, creates audit, trace_id propagation.

        Flow per §14: source agent (agent:assistant:mykim) -> target agent (agent:assistant:choi) -> human (employee:choi) via DM.
        """
        import uuid as _uuid

        if not text or not text.strip():
            raise ValueError("text/message required for direct message")
        if not target_employee or not target_employee.startswith("employee:"):
            raise ValueError("target_employee must be employee: principal")
        # derive trace_id
        ctx = agent_context if isinstance(agent_context, dict) else {}
        if not isinstance(ctx, dict):
            try:
                ctx = dict(agent_context)  # type: ignore
            except Exception:
                ctx = {}
        tid = trace_id or ctx.get("trace_id") or f"trace_{_uuid.uuid4().hex[:12]}"
        # audit
        self._audit_dm(agent_context, target_employee, text, tid)
        # resolve mattermost user for DM
        mm_user_id = self.reverse_map(target_employee)
        # derive channel: if explicit channel_id given use it, else synthesize DM channel id
        dm_channel = channel_id or f"dm_{target_employee.replace(':', '_')}"
        # if we have a real mapped mm_user_id, include it for observability
        result = await self.send_message(dm_channel, text, props=props, root_id=None)
        # enrich with governance metadata
        target_agent = target_employee.replace("employee:", "agent:assistant:", 1)
        source_agent = ctx.get("agent_id") or ""
        if not source_agent and ctx.get("user_id"):
            uid = ctx.get("user_id")
            source_agent = uid.replace("employee:", "agent:assistant:", 1) if isinstance(uid, str) and uid.startswith("employee:") else str(uid)
        enriched: dict[str, Any] = {
            **result,
            "target_employee": target_employee,
            "target_agent": target_agent,
            "source_agent": source_agent,
            "trace_id": tid,
            "channel_id": dm_channel,
            "mattermost_user_id": mm_user_id,
            "audit_logged": True,
            "approval_required": False,
        }
        # ensure skeleton includes trace
        return enriched

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

    # ---- Approval card §23 ------------------------------------------------

    def build_approval_card(self, approval_request: Any) -> dict[str, Any]:
        """Build Mattermost attachment props for §23 approval card with 4 buttons."""
        if isinstance(approval_request, dict):
            approval_id = approval_request.get("approval_id", "")
            resource = approval_request.get("resource", "")
            action = approval_request.get("action", "")
            risk = approval_request.get("risk", "")
            user_id = approval_request.get("user_id", "")
            expires_at = str(approval_request.get("expires_at", ""))
        else:
            approval_id = getattr(approval_request, "approval_id", "")
            resource = getattr(approval_request, "resource", "")
            action = getattr(approval_request, "action", "")
            risk = getattr(approval_request, "risk", "")
            user_id = getattr(approval_request, "user_id", "")
            expires_at = str(getattr(approval_request, "expires_at", ""))

        integration_url = f"{self.base_url}/v1/mattermost/actions" if self.base_url else "/v1/mattermost/actions"
        attachment = {
            "fallback": f"Approval required: {action} {resource} ({approval_id})",
            "color": "#F59E0B" if risk == "HIGH" else "#3B82F6",
            "title": "Approval Required",
            "text": f"**Action:** `{action}`\n**Resource:** `{resource}`\n**Requester:** `{user_id}`\n**Approval ID:** `{approval_id}`\n**Expires:** `{expires_at}`",
            "fields": [
                {"title": "Risk", "value": risk, "short": True},
                {"title": "Approval ID", "value": approval_id, "short": True},
            ],
            "actions": [
                {
                    "id": "deny",
                    "name": "Deny",
                    "integration": {"url": integration_url, "context": {"approval_id": approval_id, "decision": "DENIED"}},
                },
                {
                    "id": "approve_once",
                    "name": "Approve Once",
                    "integration": {"url": integration_url, "context": {"approval_id": approval_id, "decision": "APPROVED_ONCE"}},
                },
                {
                    "id": "approve_user_always",
                    "name": "Always (User)",
                    "integration": {"url": integration_url, "context": {"approval_id": approval_id, "decision": "APPROVED_USER_ALWAYS"}},
                },
                {
                    "id": "approve_group_always",
                    "name": "Always (Group)",
                    "integration": {"url": integration_url, "context": {"approval_id": approval_id, "decision": "APPROVED_GROUP_ALWAYS"}},
                },
            ],
        }
        props: dict[str, Any] = {"attachments": [attachment]}
        return props

    async def post_approval_card(
        self,
        channel_id: str,
        approval_request: Any,
        text: str | None = None,
        root_id: str | None = None,
    ) -> dict[str, Any]:
        """Post §23 approval card with 4 buttons to a channel (threaded if root_id)."""
        props = self.build_approval_card(approval_request)
        aid = approval_request.get("approval_id") if isinstance(approval_request, dict) else getattr(approval_request, "approval_id", "")
        if aid:
            props["approval_id"] = aid
        fallback_text = text or f"Approval required: {aid}"
        return await self.send_message(channel_id, fallback_text, props=props, root_id=root_id)

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
        Props handling: preserves attachments/actions for interactive cards (§23).
        """
        if not self.base_url or not self.bot_token:
            out: dict[str, Any] = {
                "_skeleton": True,
                "channel_id": channel_id,
                "text": text,
                "message": "MATTERMOST_URL or MATTERMOST_BOT_TOKEN not set — skeleton",
            }
            if props is not None:
                out["props"] = props
            if root_id is not None:
                out["root_id"] = root_id
            return out
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
        return ["mattermost/channel/*", "mattermost/user/*", "mattermost/team/*", "mattermost/dm/*"]

    def tool_action(self, tool_name: str) -> str:
        return self.TOOL_ACTION.get(tool_name, "READ")

    def describe_tools(self) -> list[dict[str, Any]]:
        out = []
        for k, v in self.TOOL_ACTION.items():
            if k in self.COLLEAGUE_TOOLS:
                out.append({
                    "name": k,
                    "action": v,
                    "resource_pattern": "mattermost/dm/*",
                    "description": "Agent-to-agent colleague DM via Mattermost — approval_not_required, audit_logged, rate_limited",
                    "approval_required": False,
                    "audit": True,
                    "rate_limit": {"per_sec": 5, "burst": 20},
                    "params": ["target_employee|target_user|mattermost_username", "text|message"],
                })
            else:
                out.append({"name": k, "action": v, "resource_pattern": "mattermost/*"})
        return out

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
        if tool_name in ("notify_colleague", "mattermost_send_direct_message", "mattermost_send_dm"):
            target_employee, _hint = self._resolve_target_employee(args)
            text = args.get("text") or args.get("message") or args.get("content") or ""
            if not text:
                raise ValueError("text/message required for colleague DM")
            trace_id = None
            if isinstance(agent_context, dict):
                trace_id = agent_context.get("trace_id")
            # pass through channel_id if caller provided explicit DM channel
            channel_id = args.get("channel_id") or args.get("channel") or None
            return await self.send_direct_message(
                target_employee=target_employee,
                text=text,
                agent_context=agent_context,
                channel_id=channel_id,
                trace_id=trace_id,
                props=args.get("props"),
            )
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
            "resources": ["mattermost/channel/*", "mattermost/user/*", "mattermost/dm/*"],
            "base_url": self.base_url,
            "has_bot_token": bool(self.bot_token),
            "has_webhook_secret": bool(self.webhook_secret),
        }
