"""Risk Classification — Section 21 확장

Deterministic, never LLM-based (Section 45).
LOW / MEDIUM / HIGH 세 단계 + Section 21 세부 규칙:

HIGH:
 - external send/share (is_external=True 또는 resource에 external)
 - delete / deploy / merge / pay / permission change / admin
 - bulk export / bulk read (resource에 bulk, export, download, PII)
 - PII export / SECRET 분류

MEDIUM:
 - 내부 문서 read (outline, drive 등)
 - wiki write, issue create, calendar write, task update
 - CREATE / MODIFY / EXECUTE

LOW:
 - 일반 검색/요약/계산, public web, non-sensitive read
"""
from __future__ import annotations

from enum import Enum

try:
    from common_types.types import RiskLevel as CommonRiskLevel  # type: ignore
except Exception:  # fallback when package not installed as distribution
    CommonRiskLevel = None  # type: ignore


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# Section 21 — 결정적 매핑
HIGH_RISK_ACTIONS = {"SEND", "DELETE", "DEPLOY", "MERGE", "PAY", "EXPORT", "SHARE", "ADMIN"}
MEDIUM_RISK_ACTIONS = {"CREATE", "MODIFY", "EXECUTE"}

# bulk/PII/external 감지 키워드
_BULK_KEYWORDS = frozenset({"bulk", "all", "export", "download", "full_dump"})
_PII_KEYWORDS = frozenset({"pii", "ssn", "personal", "고객", "주민등록", "phone", "email_bulk"})
_EXTERNAL_KEYWORDS = frozenset({"external", "outside", "third_party", "public_share"})
_SECRET_KEYWORDS = frozenset({"secret", "credential", "password", "token", "private_key"})


def _contains_keyword(resource: str, keywords: frozenset[str]) -> bool:
    low = resource.lower()
    return any(k in low for k in keywords)


def classify(
    action: str,
    resource: str,
    is_external: bool = False,
    data_classification: str | None = None,
    arg_hints: dict | None = None,
) -> RiskLevel:
    """Section 21 risk classification — deterministic.

    Args:
        action: canonical action (대문자, 예: SEND, READ)
        resource: canonical resource 문자열
        is_external: 외부 전송 여부 (Section 29 egress)
        data_classification: PUBLIC/INTERNAL/CONFIDENTIAL/PII/SECRET
        arg_hints: tool args 힌트 (예: {"recipient": "external@...", "bulk": True})

    Returns:
        RiskLevel enum
    """
    action_upper = (action or "").strip().upper()
    resource_str = resource or ""

    # ── HIGH 판정 (Section 21) ──────────────────────────────────────
    # 1. 고위험 action
    if action_upper in HIGH_RISK_ACTIONS:
        return RiskLevel.HIGH

    # 2. external 전송/공유 — Section 29 egress
    if is_external:
        return RiskLevel.HIGH
    if _contains_keyword(resource_str, _EXTERNAL_KEYWORDS):
        return RiskLevel.HIGH
    if arg_hints and arg_hints.get("is_external"):
        return RiskLevel.HIGH
    if arg_hints and isinstance(arg_hints.get("recipient"), str):
        recipient = arg_hints["recipient"]
        # 외부 도메인으로의 SEND는 HIGH
        if "@" in recipient and not recipient.endswith("@internal.example.com"):
            # 단순 휴리스틱: 내부가 아닌 recipient → HIGH
            if action_upper in ("SEND", "SHARE", "EXPORT"):
                return RiskLevel.HIGH

    # 3. bulk / full export 감지
    low_res = resource_str.lower()
    if "bulk" in low_res or "export" in low_res or "download" in low_res:
        return RiskLevel.HIGH
    if arg_hints and arg_hints.get("bulk") is True:
        return RiskLevel.HIGH
    if arg_hints and arg_hints.get("limit") is not None:
        try:
            if int(arg_hints["limit"]) > 100:
                # 대량 조회는 HIGH
                return RiskLevel.HIGH
        except Exception:
            pass

    # 4. PII / SECRET 데이터 classification
    if data_classification in ("PII", "SECRET"):
        # PII/SECRET 읽기 자체는 MEDIUM이지만, SEND/EXPORT/SHARE면 이미 HIGH 위에서 잡힘
        # 대량 PII 읽기만 HIGH로 승격
        if _contains_keyword(resource_str, _PII_KEYWORDS) or _contains_keyword(resource_str, _SECRET_KEYWORDS):
            return RiskLevel.HIGH
        # classification만으로는 MEDIUM으로 유지 (아래 MEDIUM 분기)

    # 5. PII 키워드가 resource에 포함
    if _contains_keyword(resource_str, _PII_KEYWORDS):
        return RiskLevel.HIGH
    if _contains_keyword(resource_str, _SECRET_KEYWORDS):
        return RiskLevel.HIGH

    # ── MEDIUM 판정 ─────────────────────────────────────────────────
    if action_upper in MEDIUM_RISK_ACTIONS:
        return RiskLevel.MEDIUM
    # 내부 문서 read — outline, drive, notion 등
    if action_upper in ("READ", "SEARCH") and any(
        kw in low_res for kw in ("outline", "notion", "wiki", "drive", "calendar", "tasks")
    ):
        # PII/SECRET가 아니면 MEDIUM, PUBLIC이면 LOW로 내려갈 수 있으나 기본 MEDIUM
        if data_classification == "PUBLIC":
            return RiskLevel.LOW
        if data_classification in ("PII", "SECRET"):
            return RiskLevel.MEDIUM
        return RiskLevel.MEDIUM
    # calendar/tasks write는 MEDIUM (이미 HIGH action 아니면)
    if action_upper in ("CREATE", "MODIFY") and any(kw in low_res for kw in ("calendar", "tasks", "outline")):
        return RiskLevel.MEDIUM

    # ── LOW (기본) ─────────────────────────────────────────────────
    # 일반 검색/요약/계산, public web, non-sensitive read
    return RiskLevel.LOW


def requires_capability(risk: RiskLevel) -> bool:
    """해당 risk에서 capability token이 필요한지 여부."""
    return risk == RiskLevel.HIGH


# 하위호환: 기존 코드가 classify(action, resource, is_external=...) 형태로 호출
__all__ = ["RiskLevel", "classify", "requires_capability", "HIGH_RISK_ACTIONS", "MEDIUM_RISK_ACTIONS"]
