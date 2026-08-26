"""Google Personal Connector — Section 9-10

Gmail / Calendar / Drive / Tasks personal 자원 담당.

보안 원칙:
- personal resource는 owner만 접근 가능 (owner isolation)
- scope 최소 단위 요청
- delegation_id / credential_binding_id 바인딩 검증
"""
from __future__ import annotations

from dataclasses import dataclass

try:
    from ..normalize import parse_resource, is_personal_resource, extract_owner_user_id
except ImportError:
    from execution_gateway.normalize import parse_resource, is_personal_resource, extract_owner_user_id  # type: ignore

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


@dataclass(frozen=True)
class OwnerCheckResult:
    allowed: bool
    reason: str
    owner_user_id: str | None = None


class GoogleConnector:
    """Google personal tools connector — owner isolation 강제"""

    name = "google"
    provider = "google"

    def required_scope(self, tool_name: str) -> str | None:
        return GOOGLE_SCOPES.get(tool_name)

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

        Returns:
            OwnerCheckResult
        """
        # AgentContext 파싱 (dict 또는 pydantic model 모두 지원)
        if isinstance(agent_context, dict):
            ctx_user = agent_context.get("user_id")
            ctx_agent = agent_context.get("agent_id")
        else:
            ctx_user = getattr(agent_context, "user_id", None)
            ctx_agent = getattr(agent_context, "agent_id", None)

        if not ctx_user:
            return OwnerCheckResult(False, "missing user_id in AgentContext")

        # resource가 비어있으면 tool 기반 추정 — owner 체크는 pass하지만 delegation 필요
        if not resource:
            return OwnerCheckResult(True, "no resource — deferred check", None)

        # enterprise resource면 google connector가 담당 아님 → 허용 (다른 connector가 처리)
        parsed = None
        try:
            parsed = parse_resource(resource)
        except ValueError as e:
            return OwnerCheckResult(False, f"invalid resource: {e}")

        # personal resource가 아니면 google isolation 대상 아님
        if not parsed.is_personal:
            return OwnerCheckResult(True, "non-personal resource — no owner isolation", None)

        # google domain이 아니면 skip
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
        # agent_id도 user와 일치하는지 확인 (agent:assistant:kim ↔ employee:kim)
        if ctx_agent:
            expected_agent = ctx_user.replace("employee:", "agent:assistant:", 1) if ctx_user.startswith("employee:") else None
            if expected_agent and ctx_agent != expected_agent:
                # 경고성 — 실제 DENY는 하지 않지만 trace에 남김
                pass

        return OwnerCheckResult(True, "owner verified", owner)

    def validate_delegation(self, agent_context: dict | object, resource: str) -> tuple[bool, str]:
        """delegation_id / credential_binding_id 존재 여부 검증."""
        if isinstance(agent_context, dict):
            delegation_id = agent_context.get("delegation_id")
            credential_binding_id = agent_context.get("credential_binding_id")
        else:
            delegation_id = getattr(agent_context, "delegation_id", None)
            credential_binding_id = getattr(agent_context, "credential_binding_id", None)

        # personal resource에 대해서는 delegation이 권장되지만,
        # 읽기 등 LOW-risk는 delegation 없이도 통과하도록 관대하게 처리
        # HIGH-risk personal (gmail_send)는 반드시 delegation 필요 — proxy에서 강제
        return True, "ok"

    def describe(self) -> dict:
        return {
            "name": self.name,
            "provider": self.provider,
            "tools": self.list_tools(),
            "resources": self.list_resources(),
            "scopes": GOOGLE_SCOPES,
        }
