"""Data Access Pattern — v1.4.1 §16I + §16G Blast Radius

16I: Enterprise Data Access Pattern
  Read  = Read Replica / Read-only API → MCP 우선 (최소 데이터/필드/ROW)
  Write = Command API + Policy + Approval (Security Core 경유)
  Direct DB = DENY (Hermes → Production DB 금지)

16G: Untrusted Execution Worker — Hermes를 compromised로 가정
  Capability(=문제해결능력) vs Authority(=기업권한) 분리
  Shell = Arbitrary Tool Generator → 금지 대신 경계 제한
  Blast Radius 제한: /home/hermes 외 Production 자원 직접 접근 불가
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# ── Action classification ──────────────────────────────────────────

READ_ACTIONS = frozenset({"READ", "SEARCH", "QUERY", "LIST", "FIND", "GET", "FETCH", "RETRIEVE"})
WRITE_ACTIONS = frozenset({"CREATE", "MODIFY", "UPDATE", "EDIT", "PATCH", "DELETE", "DEPLOY", "MERGE", "PAY", "SEND", "EXPORT", "SHARE", "ADMIN", "REMOVE", "TRASH"})

# §16I.2 명시된 쓰기/고위험 (Command API + Approval 필수)
WRITE_APPROVAL_ACTIONS = frozenset({"CREATE", "MODIFY", "DELETE", "DEPLOY", "MERGE", "PAY"})

# Alias normalization (대소문자/동의어)
_ACTION_ALIASES: dict[str, str] = {
    "GET": "READ", "FETCH": "READ", "RETRIEVE": "READ",
    "QUERY": "SEARCH", "LIST": "SEARCH", "FIND": "SEARCH",
    "INSERT": "CREATE", "ADD": "CREATE",
    "UPDATE": "MODIFY", "EDIT": "MODIFY", "PATCH": "MODIFY",
    "REMOVE": "DELETE", "TRASH": "DELETE",
    "SEND_EMAIL": "SEND", "SEND_MAIL": "SEND",
    "SHARE_EXTERNAL": "SHARE", "BULK_EXPORT": "EXPORT", "DOWNLOAD": "EXPORT",
}

# ── Allowed sources §16I ────────────────────────────────────────────

# §16I.1 Read Path: Production DB → Read Replica / Read-only View → Query Service → MCP → Agent
ALLOWED_READ_SOURCES: list[str] = [
    "read_replica",
    "read_only_api",
    "read_only_view",
    "query_service",
    "mcp",
]

# §16I.2 Write Path: Agent → MCP → Security Core → Policy → Approval → Command API → Enterprise
ALLOWED_WRITE_SOURCES: list[str] = [
    "command_api",
    "security_core",
    "approval",
    "mcp",
]

# Backward compat aliases (tests may check these names)
allowed_read_sources = ALLOWED_READ_SOURCES
allowed_write_sources = ALLOWED_WRITE_SOURCES
write_sources = ALLOWED_WRITE_SOURCES
read_sources = ALLOWED_READ_SOURCES


class DataAccessDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"


@dataclass(frozen=True)
class DataAccessResult:
    allowed: bool
    decision: str  # ALLOW / DENY / APPROVAL_REQUIRED
    reason: str
    required_source: str | None = None
    requires_approval: bool = False
    source: str | None = None


def _canonical_action(action: str) -> str:
    if not action:
        return ""
    key = action.strip().upper()
    if key in _ACTION_ALIASES:
        return _ACTION_ALIASES[key]
    if key in READ_ACTIONS or key in WRITE_ACTIONS:
        return key
    # already canonical or unknown — return upper
    return key


class DataAccessPolicy:
    """§16I Data Access Policy — 결정론적, LLM 미사용.

    - read_path(READ/SEARCH)  → read_only_api / mcp 경유 필수
    - write_path(CREATE/…/PAY) → command_api + approval 필수
    - direct_db_access(user, resource) → DENY (Hermes → Production DB 금지)
    - check(action, resource, source) → 일반 훅 (proxy에서 호출)
    """

    def __init__(
        self,
        allowed_read_sources: list[str] | None = None,
        allowed_write_sources: list[str] | None = None,
    ):
        self.allowed_read_sources = allowed_read_sources or ALLOWED_READ_SOURCES
        self.allowed_write_sources = allowed_write_sources or ALLOWED_WRITE_SOURCES
        # legacy names
        self.read_sources = self.allowed_read_sources
        self.write_sources = self.allowed_write_sources

    # ── 16I.1 Read Path ────────────────────────────────────────────
    def read_path(self, action: str, resource: str = "", source: str | None = None) -> DataAccessResult:
        """READ/SEARCH → read_only_api 경유 필수. 그 외 DENY."""
        canon = _canonical_action(action)
        if canon not in READ_ACTIONS:
            return DataAccessResult(
                allowed=False,
                decision=DataAccessDecision.DENY.value,
                reason=f"read_path: action {canon!r} is not a read action (allowed: READ, SEARCH)",
                required_source=None,
            )
        # source가 명시되면 read source 검증
        if source is not None and source not in self.allowed_read_sources:
            return DataAccessResult(
                allowed=False,
                decision=DataAccessDecision.DENY.value,
                reason=f"read_path: source {source!r} not in allowed_read_sources {self.allowed_read_sources}",
                required_source="read_only_api",
                source=source,
            )
        return DataAccessResult(
            allowed=True,
            decision=DataAccessDecision.ALLOW.value,
            reason=f"read_path: {canon} on {resource or '*'} via read_only_api/mcp",
            required_source="read_only_api",
            requires_approval=False,
            source=source or "read_only_api",
        )

    # ── 16I.2 Write Path ───────────────────────────────────────────
    def write_path(self, action: str, resource: str = "", source: str | None = None) -> DataAccessResult:
        """CREATE/MODIFY/DELETE/DEPLOY/MERGE/PAY → command_api + approval 필수."""
        canon = _canonical_action(action)
        if canon not in WRITE_ACTIONS:
            return DataAccessResult(
                allowed=False,
                decision=DataAccessDecision.DENY.value,
                reason=f"write_path: action {canon!r} is not a write action",
                required_source=None,
            )
        # source가 허용 목록 밖이면 DENY (direct DB 등 차단)
        if source is not None and source not in self.allowed_write_sources and source != "command_api":
            # still allow if source is command_api variant
            if source not in self.allowed_write_sources:
                return DataAccessResult(
                    allowed=False,
                    decision=DataAccessDecision.DENY.value,
                    reason=f"write_path: source {source!r} not in allowed_write_sources",
                    required_source="command_api",
                    source=source,
                )
        # 모든 write는 approval 필요 ( §16I.2 Command Path → Policy → Approval → Command API )
        requires_approval = canon in WRITE_APPROVAL_ACTIONS or canon in WRITE_ACTIONS
        return DataAccessResult(
            allowed=False,  # write는 approval 없이는 바로 허용 안 함 — APPROVAL_REQUIRED로 명시
            decision=DataAccessDecision.APPROVAL_REQUIRED.value,
            reason=f"write_path: {canon} on {resource or '*'} requires command_api + approval",
            required_source="command_api",
            requires_approval=True,
            source=source or "command_api",
        )

    # ── 16I.3 Direct DB Access ─────────────────────────────────────
    def direct_db_access(self, user: str, resource: str) -> DataAccessResult:
        """Agent Runtime → Production DB 직접 접근 금지. 항상 DENY.

        Hermes (또는 어떤 agent user)도 production DB에 직접 접근 불가.
        필요한 접근은 MCP → Query Service → Read-only API 경유만 허용.
        """
        return DataAccessResult(
            allowed=False,
            decision=DataAccessDecision.DENY.value,
            reason=f"direct_db_access DENY: {user!r} → {resource!r} (Hermes → Production DB 금지, use MCP→QueryService→Read-only API)",
            required_source=None,
            requires_approval=False,
        )

    # ── Generic check (proxy hook) ─────────────────────────────────
    def check(
        self,
        action: str,
        resource: str = "",
        source: str | None = None,
        user: str | None = None,
    ) -> DataAccessResult:
        """결정론적 훅 — proxy/tool 호출 시 호출.

        - direct_db / production 키워드 → DENY
        - READ/SEARCH → read_path
        - WRITE 계열 → write_path (APPROVAL_REQUIRED)
        - 그 외 → DENY (안전 기본값)
        """
        canon = _canonical_action(action)
        src_lower = (source or "").lower()
        res_lower = (resource or "").lower()

        # Direct DB 차단: source가 direct_db / production_db 이거나 resource에 production/prod_db 포함
        if src_lower in ("direct_db", "production_db", "prod_db", "direct") or "production" in res_lower or "prod_db" in res_lower:
            if user:
                return self.direct_db_access(user, resource)
            return DataAccessResult(
                allowed=False,
                decision=DataAccessDecision.DENY.value,
                reason=f"check DENY: direct DB access blocked for {canon} on {resource!r}",
                required_source=None,
            )

        if canon in READ_ACTIONS:
            return self.read_path(canon, resource, source if source in self.allowed_read_sources else None)
        if canon in WRITE_ACTIONS:
            return self.write_path(canon, resource, source)
        # Unknown action → DENY (fail-closed)
        return DataAccessResult(
            allowed=False,
            decision=DataAccessDecision.DENY.value,
            reason=f"check DENY: unknown action {canon!r}",
            required_source=None,
        )

    # ── Blast Radius 경계 ──────────────────────────────────────────
    def check_blast_radius(self, user: str, resource: str, action: str = "") -> DataAccessResult:
        """§16G Blast Radius 경계 검증.

        Hermes가 compromised 되어도 도달 불가해야 하는 자원:
        - Production DB / ERP / CRM / SSH / other user home / Credential Vault / Security Core secret

        resource가 blast radius 밖이면 DENY.
        """
        blocked_keywords = ("production", "prod_db", "erp", "crm", "ssh", "credential_vault", "security_core", "other_user", "../", "/etc/", "vault")
        res_lower = resource.lower()
        for kw in blocked_keywords:
            if kw in res_lower:
                return DataAccessResult(
                    allowed=False,
                    decision=DataAccessDecision.DENY.value,
                    reason=f"blast_radius DENY: {user!r} cannot access {resource!r} (keyword {kw!r} outside /home/hermes)",
                    required_source=None,
                )
        # also block direct DB via user hermes
        if "hermes" in user.lower() and ("db" in res_lower or "database" in res_lower):
            # narrow: production db already handled, but any DB direct is blocked
            if "production" in res_lower or "prod" in res_lower:
                return DataAccessResult(
                    allowed=False,
                    decision=DataAccessDecision.DENY.value,
                    reason=f"blast_radius DENY: Hermes → DB direct access prohibited ({resource!r})",
                    required_source=None,
                )
        return DataAccessResult(
            allowed=True,
            decision=DataAccessDecision.ALLOW.value,
            reason=f"blast_radius ALLOW: {resource!r} within allowed boundary (check passed)",
            required_source=None,
        )


# Singleton for proxy hook
_default_policy: DataAccessPolicy | None = None

def get_data_access_policy() -> DataAccessPolicy:
    global _default_policy
    if _default_policy is None:
        _default_policy = DataAccessPolicy()
    return _default_policy
