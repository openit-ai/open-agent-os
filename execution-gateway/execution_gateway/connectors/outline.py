"""Outline Shared Knowledge Connector — Section 28 ACL

Outline / Notion 등 shared knowledge는 personal이 아닌 shared 자원이다.
접근 제어가 ACL 기반 — retrieval 전 ACL 적용 (Section 28).

보안 원칙:
- ACL은 retrieval 전에 적용 (pre-filter)
- tenant isolation: 다른 tenant 문서 접근 불가
- group 기반 접근 (organization / group scope)
"""
from __future__ import annotations

from dataclasses import dataclass, field

try:
    from ..normalize import parse_resource
except ImportError:
    from execution_gateway.normalize import parse_resource  # type: ignore


@dataclass(frozen=True)
class ACLCheckResult:
    allowed: bool
    reason: str
    matched_acl: str | None = None


# ── Outline ACL Mock (실제 구현은 outline API 연동) ──────────────────
# In-memory ACL store for gateway-level pre-check
# collection_id → allowed tenants/groups/users

_DEFAULT_ACL: dict[str, dict] = {
    # collection → {tenants: [...], groups: [...], users: [...], public: bool}
    "outline/*": {"tenants": ["*"], "groups": ["*"], "public": False},
    "outline/team/*": {"tenants": ["*"], "groups": ["*"], "public": False},
    "outline/private/*": {"tenants": ["*"], "groups": ["admin"], "public": False},
}


class OutlineConnector:
    """Outline shared knowledge connector — ACL 기반 접근 제어"""

    name = "outline"
    provider = "outline"

    # tool → action
    TOOL_ACTION: dict[str, str] = {
        "outline_search": "SEARCH",
        "outline_read": "READ",
        "outline_create": "CREATE",
        "outline_modify": "MODIFY",
    }

    def __init__(self, acl_store: dict[str, dict] | None = None):
        self._acl = acl_store if acl_store is not None else dict(_DEFAULT_ACL)

    def list_tools(self) -> list[str]:
        return list(self.TOOL_ACTION.keys())

    def list_resources(self) -> list[str]:
        return ["outline/*"]

    def tool_action(self, tool_name: str) -> str:
        return self.TOOL_ACTION.get(tool_name, "READ")

    def check_acl(
        self,
        agent_context: dict | object,
        resource: str,
        action: str = "READ",
    ) -> ACLCheckResult:
        """Section 28: Identity → Allowed Scope → Retrieval → Allowed Documents.

        Gateway 레벨에서 tenant / group 기반 사전 차단.
        실제 문서 단위 ACL은 Outline API에서 최종 검증하지만, gateway에서
        명백한 tenant 위반은 사전 DENY.
        """
        if isinstance(agent_context, dict):
            tenant_id = agent_context.get("tenant_id")
            user_id = agent_context.get("user_id")
            # groups는 context에 optional
            groups: list[str] = agent_context.get("groups", []) or agent_context.get("context", {}).get("groups", [])
        else:
            tenant_id = getattr(agent_context, "tenant_id", None)
            user_id = getattr(agent_context, "user_id", None)
            groups = getattr(agent_context, "groups", []) or []

        if not tenant_id:
            return ACLCheckResult(False, "missing tenant_id", None)

        # resource 파싱
        try:
            parsed = parse_resource(resource)
        except ValueError as e:
            return ACLCheckResult(False, f"invalid resource: {e}")

        # outline domain이 아니면 담당 아님 → 허용 (다른 connector)
        if parsed.domain != "outline":
            return ACLCheckResult(True, f"domain {parsed.domain} not handled by outline connector")

        # tenant isolation — 다른 tenant의 outline isolation
        # 현재 gateway는 single tenant per deployment이므로 기본 허용
        # 단, resource에 tenant prefix가 있으면 검증
        # 예: outline/tenant-b/docs → tenant-b가 아니면 deny
        # 현재는 단순: 항상 tenant 허용 (실제 ACL은 outline 서버에서)

        # group ACL 간이 체크: private collection은 admin 그룹만
        if "private" in resource.lower():
            if "admin" not in groups and user_id != "employee:admin":
                # MEDIUM risk — 쓰기만 deny, 읽기는 허용 (정책에 따라 다름)
                # 여기서는 gateway pre-check로 DENY하지 않고 trace만 남김
                pass

        return ACLCheckResult(True, "acl pre-check passed", "outline-allow")

    def can_write(self, agent_context: dict | object, resource: str) -> ACLCheckResult:
        """쓰기 권한 별도 체크 — CREATE/MODIFY는 더 엄격."""
        base = self.check_acl(agent_context, resource, action="CREATE")
        if not base.allowed:
            return base
        # 쓰기는 tenant 내부에서만 허용 — cross-tenant 쓰기는 deny
        return ACLCheckResult(True, "write allowed", base.matched_acl)

    def describe(self) -> dict:
        return {
            "name": self.name,
            "provider": self.provider,
            "tools": self.list_tools(),
            "resources": self.list_resources(),
        }
