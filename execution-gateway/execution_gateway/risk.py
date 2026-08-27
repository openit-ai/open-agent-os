"""Risk Classification — Section 21 + Section 29 확장

Deterministic, never LLM-based (Section 45).
LOW / MEDIUM / HIGH 세 단계 + Section 21 세부 규칙 + Section 29 Data Classification 5단계

HIGH:
 - external send/share (is_external=True 또는 resource에 external)
 - delete / deploy / merge / pay / permission change / admin
 - bulk export / bulk read (resource에 bulk, export, download, PII)
 - PII export / SECRET 분류
 - §29 HIGH Egress: EXPORT / BULK / PII / SECRET (is_external + classification)

MEDIUM:
 - 내부 문서 read (outline, drive 등)
 - wiki write, issue create, calendar write, task update
 - CREATE / MODIFY / EXECUTE

LOW:
 - 일반 검색/요약/계산, public web, non-sensitive read

Section 29: 5단계 분류 (PUBLIC/INTERNAL/CONFIDENTIAL/PII/SECRET)
 - content hook 기반 자동 분류
 - HIGH Egress 판정: EXPORT/BULK/PII/SECRET + external
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any

try:
    from common_types.types import RiskLevel as CommonRiskLevel  # type: ignore
except Exception:  # fallback when package not installed as distribution
    CommonRiskLevel = None  # type: ignore

try:
    from common_types.types import DataClassification as CommonDC  # type: ignore
except Exception:
    CommonDC = None  # type: ignore


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class DataClassification(str, Enum):
    """§29 5단계 데이터 분류."""
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    PII = "PII"
    SECRET = "SECRET"


# Section 21 — 결정적 매핑 (+ §16H BULK/EXPORT escalation)
HIGH_RISK_ACTIONS = {
    "SEND", "DELETE", "DEPLOY", "MERGE", "PAY", "EXPORT", "SHARE", "ADMIN",
    # §16H.3 — bulk/export/external must escalate to HIGH (tool_policy 연동)
    "BULK_READ", "BULK_DOWNLOAD", "BULK_EXPORT", "SHARE_EXTERNAL", "SEND_EXTERNAL",
}
MEDIUM_RISK_ACTIONS = {"CREATE", "MODIFY", "EXECUTE"}

# bulk/PII/external 감지 키워드
_BULK_KEYWORDS = frozenset({"bulk", "all", "export", "download", "full_dump"})
_PII_KEYWORDS = frozenset({"pii", "ssn", "personal", "고객", "주민등록", "phone", "email_bulk"})
_EXTERNAL_KEYWORDS = frozenset({"external", "outside", "third_party", "public_share"})
_SECRET_KEYWORDS = frozenset({"secret", "credential", "password", "token", "private_key"})

# §29 HIGH Egress triggers
HIGH_EGRESS_ACTIONS = frozenset({"EXPORT", "SHARE", "SEND"})
HIGH_EGRESS_CLASSIFICATIONS = frozenset({"PII", "SECRET"})

# Content-based classification patterns (§29 hook)
_PII_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN
    re.compile(r"\b\d{6}-\d{7}\b"),  # Korean RRN
    re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),  # email (bulk context)
    re.compile(r"\b\d{2,3}-\d{3,4}-\d{4}\b"),  # phone
    re.compile(r"(주민등록|개인정보|고객정보)", re.IGNORECASE),
]
_SECRET_PATTERNS: list[re.Pattern] = [
    re.compile(r"(password|passwd|secret|credential|private.?key|api.?key|token)", re.IGNORECASE),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"-----BEGIN (RSA )?PRIVATE KEY-----"),
]
_CONFIDENTIAL_KEYWORDS = frozenset({"confidential", "기밀", "내부용", "restricted", "sensitive"})


def _contains_keyword(resource: str, keywords: frozenset[str]) -> bool:
    low = resource.lower()
    return any(k in low for k in keywords)


# ── §29: Content-based classification hook ─────────────────────────

def classify_content(
    content: str | None,
    resource: str | None = None,
    hint: str | None = None,
) -> DataClassification:
    """§29 resource 내용 기반 분류 hook — deterministic.

    Priority: SECRET > PII > CONFIDENTIAL > INTERNAL > PUBLIC
    - SECRET: credential / private key / token patterns
    - PII: SSN / RRN / phone / personal info patterns
    - CONFIDENTIAL: confidential keywords
    - PUBLIC: explicit public hint or public/web resource
    - INTERNAL: default
    """
    text = (content or "") + " " + (resource or "") + " " + (hint or "")
    if not text.strip():
        return DataClassification.INTERNAL

    # SECRET patterns
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            return DataClassification.SECRET

    # resource-level secret keywords
    if resource and _contains_keyword(resource, _SECRET_KEYWORDS):
        return DataClassification.SECRET

    # PII patterns — need to be careful not to over-match single email
    for pat in _PII_PATTERNS:
        if pat.search(text):
            # single email alone is not necessarily PII bulk, but SSN/RRN/phone is
            # For deterministic behavior: any PII pattern → PII
            # But confidential keyword + PII → still PII
            return DataClassification.PII
    if resource and _contains_keyword(resource, _PII_KEYWORDS):
        return DataClassification.PII
    # content has PII keywords
    if _contains_keyword(text, _PII_KEYWORDS):
        return DataClassification.PII

    # CONFIDENTIAL
    if _contains_keyword(text, _CONFIDENTIAL_KEYWORDS):
        return DataClassification.CONFIDENTIAL

    # PUBLIC hint
    low = text.lower()
    if hint == "PUBLIC" or (resource and "public" in resource.lower()) or "public/web" in low:
        return DataClassification.PUBLIC

    return DataClassification.INTERNAL


def classify_data(
    content: str | None = None,
    resource: str | None = None,
    hint: str | None = None,
) -> DataClassification:
    """Alias for classify_content (backward compat)."""
    return classify_content(content, resource, hint)


# ── §29: HIGH Egress 판정 ─────────────────────────────────────────

def is_high_egress(
    action: str,
    resource: str,
    data_classification: str | DataClassification | None = None,
    is_external: bool = False,
    arg_hints: dict | None = None,
) -> tuple[bool, str]:
    """§29 HIGH Egress 판정 — EXPORT/BULK/PII/SECRET + external.

    Returns:
        (is_high, reason)
    """
    action_upper = (action or "").strip().upper()
    dc_str = data_classification.value if isinstance(data_classification, Enum) else (data_classification or "")
    dc_upper = dc_str.upper() if dc_str else ""

    # 1. External send/share/export is always HIGH egress
    if is_external and action_upper in HIGH_EGRESS_ACTIONS:
        return True, f"HIGH egress: {action_upper} with is_external=True"
    if arg_hints and arg_hints.get("is_external") and action_upper in HIGH_EGRESS_ACTIONS:
        return True, "HIGH egress: is_external hint"

    # 2. PII / SECRET classification + any egress action → HIGH
    if dc_upper in HIGH_EGRESS_CLASSIFICATIONS:
        if action_upper in HIGH_EGRESS_ACTIONS or is_external:
            return True, f"HIGH egress: {dc_upper} classification with {action_upper}"
        # Bulk read of PII/SECRET is also egress risk
        if action_upper in ("READ", "SEARCH") and dc_upper in ("PII", "SECRET"):
            # Check if bulk
            if arg_hints and (arg_hints.get("bulk") or (arg_hints.get("limit") is not None and _is_bulk_limit(arg_hints.get("limit")))):
                return True, f"HIGH egress: BULK {dc_upper} read"
            if resource and _contains_keyword(resource, _BULK_KEYWORDS):
                return True, f"HIGH egress: BULK {dc_upper} resource"

    # 3. BULK export/download → HIGH egress regardless of classification
    if action_upper == "EXPORT":
        return True, "HIGH egress: EXPORT action"
    if resource and ("bulk" in resource.lower() or "export" in resource.lower()):
        if action_upper in ("READ", "SEARCH", "EXPORT", "SEND", "SHARE"):
            return True, "HIGH egress: BULK/EXPORT resource"
    if arg_hints and arg_hints.get("bulk") is True:
        return True, "HIGH egress: bulk hint"
    if arg_hints and _is_bulk_limit(arg_hints.get("limit")):
        return True, "HIGH egress: bulk limit"

    # 4. SECRET/PII keywords in resource + egress action
    if resource and _contains_keyword(resource, _SECRET_KEYWORDS) and action_upper in HIGH_EGRESS_ACTIONS:
        return True, "HIGH egress: SECRET resource with egress action"
    if resource and _contains_keyword(resource, _PII_KEYWORDS) and action_upper in HIGH_EGRESS_ACTIONS:
        return True, "HIGH egress: PII resource with egress action"

    return False, "not high egress"


def _is_bulk_limit(limit: Any) -> bool:
    if limit is None:
        return False
    try:
        return int(limit) > 100
    except Exception:
        return False


def get_egress_classification(
    action: str,
    resource: str,
    content: str | None = None,
    data_classification: str | None = None,
    is_external: bool = False,
) -> dict[str, Any]:
    """통합 egress 분석 — classification hook + high egress 판정."""
    # auto-classify if not provided
    dc = data_classification
    if dc is None and content is not None:
        dc = classify_content(content, resource).value
    elif dc is None:
        dc = classify_content(None, resource).value

    is_high, reason = is_high_egress(action, resource, dc, is_external)
    return {
        "classification": dc,
        "is_high_egress": is_high,
        "reason": reason,
        "action": action.upper() if action else "",
        "resource": resource,
    }


def classify(
    action: str,
    resource: str,
    is_external: bool = False,
    data_classification: str | None = None,
    arg_hints: dict | None = None,
    content: str | None = None,
) -> RiskLevel:
    """Section 21 + 29 risk classification — deterministic.

    Args:
        action: canonical action (대문자, 예: SEND, READ)
        resource: canonical resource 문자열
        is_external: 외부 전송 여부 (Section 29 egress)
        data_classification: PUBLIC/INTERNAL/CONFIDENTIAL/PII/SECRET (or None → auto)
        arg_hints: tool args 힌트 (예: {\"recipient\": \"external@...\", \"bulk\": True})
        content: optional resource content for §29 hook (auto-classify)

    Returns:
        RiskLevel enum
    """
    action_upper = (action or "").strip().upper()
    resource_str = resource or ""

    # Auto-classify if content provided but no explicit classification
    effective_dc = data_classification
    if effective_dc is None and content is not None:
        effective_dc = classify_content(content, resource_str).value

    # ── §29 HIGH Egress 우선 판정 ──────────────────────────────────
    is_egress, _ = is_high_egress(action_upper, resource_str, effective_dc, is_external, arg_hints)
    if is_egress:
        return RiskLevel.HIGH

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
        if "@" in recipient and not recipient.endswith("@internal.example.com"):
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
                return RiskLevel.HIGH
        except Exception:
            pass

    # 4. PII / SECRET 데이터 classification
    if effective_dc in ("PII", "SECRET"):
        if _contains_keyword(resource_str, _PII_KEYWORDS) or _contains_keyword(resource_str, _SECRET_KEYWORDS):
            return RiskLevel.HIGH

    # 5. PII 키워드가 resource에 포함
    if _contains_keyword(resource_str, _PII_KEYWORDS):
        return RiskLevel.HIGH
    if _contains_keyword(resource_str, _SECRET_KEYWORDS):
        return RiskLevel.HIGH

    # 6. Content hook — SECRET/PII content in egress context
    if content and effective_dc in ("PII", "SECRET") and action_upper in ("SEND", "SHARE", "EXPORT"):
        return RiskLevel.HIGH

    # ── MEDIUM 판정 ─────────────────────────────────────────────────
    if action_upper in MEDIUM_RISK_ACTIONS:
        return RiskLevel.MEDIUM
    if action_upper in ("READ", "SEARCH") and any(
        kw in low_res for kw in ("outline", "notion", "wiki", "drive", "calendar", "tasks")
    ):
        if effective_dc == "PUBLIC":
            return RiskLevel.LOW
        if effective_dc in ("PII", "SECRET"):
            return RiskLevel.MEDIUM
        return RiskLevel.MEDIUM
    if action_upper in ("CREATE", "MODIFY") and any(kw in low_res for kw in ("calendar", "tasks", "outline")):
        return RiskLevel.MEDIUM

    # ── LOW (기본) ─────────────────────────────────────────────────
    return RiskLevel.LOW


def requires_capability(risk: RiskLevel) -> bool:
    """해당 risk에서 capability token이 필요한지 여부."""
    return risk == RiskLevel.HIGH


# 하위호환: 기존 코드가 classify(action, resource, is_external=...) 형태로 호출
__all__ = [
    "RiskLevel",
    "DataClassification",
    "classify",
    "classify_content",
    "classify_data",
    "is_high_egress",
    "get_egress_classification",
    "requires_capability",
    "HIGH_RISK_ACTIONS",
    "MEDIUM_RISK_ACTIONS",
    "HIGH_EGRESS_ACTIONS",
    "HIGH_EGRESS_CLASSIFICATIONS",
]
