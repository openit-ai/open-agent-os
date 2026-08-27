"""Execution Gateway Tool Policy — §16H v1.4.1

Capability 단독으로는 위험한 argument/대량 호출을 막지 못함.
Tool-level 정책: argument validation + field scope + row/file limits + rate limit + bulk protection.

Deterministic, never LLM-based (§45).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


@dataclass
class ToolPolicy:
    """Per-tool policy (§16H.1).

    Example YAML:
        tool: crm.search_customer
        allowed_actions: [SEARCH]
        limits: {max_results: 50}
        allowed_fields: [company, contact_name]
        denied_fields: [password, ssn]
    """

    tool: str
    allowed_actions: list[str] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)
    allowed_fields: list[str] = field(default_factory=list)
    denied_fields: list[str] = field(default_factory=list)


# §16H.3 bulk keywords — README/10, bulk/export/download 계열
_BULK_KEYWORDS = frozenset({"bulk", "export", "download", "full_dump", "all", "bulk_read", "bulk_download"})
_BULK_ACTIONS = frozenset({"EXPORT", "BULK_READ", "BULK_DOWNLOAD", "SHARE_EXTERNAL", "SEND_EXTERNAL"})
_BULK_THRESHOLD = 100  # row count 이상이면 bulk로 간주 (§16H.3 예시: 100k는 HIGH, 10은 normal)


def is_bulk(action: str, resource: str = "", result_count: int | None = None) -> bool:
    """§16H.3 Bulk Access Protection — 별도 Capability/Risk Escalation 필요."""
    a = action.upper()
    if a in _BULK_ACTIONS:
        return True
    r = resource.lower()
    if any(k in r for k in _BULK_KEYWORDS):
        return True
    if result_count is not None and result_count >= _BULK_THRESHOLD:
        return True
    return False


def validate_tool_call(
    policy: ToolPolicy | None,
    *,
    action: str,
    args: dict[str, Any] | None = None,
    resource: str = "",
) -> tuple[bool, str]:
    """§16H.1 Tool Argument Validation.

    Returns (allowed, reason). DENY reasons are audit-friendly.
    """
    args = args or {}
    # 1. action allowlist
    if policy and policy.allowed_actions:
        allowed = {a.upper() for a in policy.allowed_actions}
        if action.upper() not in allowed:
            return False, f"action {action} not allowed for {policy.tool} (allowed: {policy.allowed_actions})"
    # 2. denied fields — always blocked
    if policy and policy.denied_fields:
        denied = {f.lower() for f in policy.denied_fields}
        for k in args.keys():
            if k.lower() in denied:
                return False, f"denied field: {k}"
        # also check requested fields list
        for f in args.get("fields", []) if isinstance(args.get("fields"), list) else []:
            if str(f).lower() in denied:
                return False, f"denied field in fields: {f}"
    # 3. allowed fields — if set, only those permitted
    if policy and policy.allowed_fields:
        allowed_f = {f.lower() for f in policy.allowed_fields}
        for f in args.get("fields", []) if isinstance(args.get("fields"), list) else []:
            if str(f).lower() not in allowed_f:
                return False, f"field not allowed: {f} (allowed: {policy.allowed_fields})"
    # 4. row limit (§16H.1 max_results)
    if policy and "max_results" in policy.limits:
        mr = int(policy.limits["max_results"])
        for key in ("limit", "max_results", "count", "page_size", "result_count"):
            if key in args:
                try:
                    if int(args[key]) > mr:
                        return False, f"{key}={args[key]} exceeds max_results={mr}"
                except (ValueError, TypeError):
                    pass
        if args.get("result_count") is not None:
            try:
                if int(args["result_count"]) > mr:
                    return False, f"result_count exceeds max_results={mr}"
            except (ValueError, TypeError):
                pass
    # 5. file size limit
    if policy and "max_file_size" in policy.limits:
        mf = int(policy.limits["max_file_size"])
        for key in ("file_size", "size", "max_file_size"):
            if key in args:
                try:
                    if int(args[key]) > mf:
                        return False, f"{key} exceeds max_file_size={mf}"
                except (ValueError, TypeError):
                    pass
    return True, "ok"


# ── Rate limit per (tenant, user, tool, resource) ─────────────────────
# §16H.2: user/agent/session/tool/resource/tenant 단위. Token-bucket lite.

@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class ToolRateLimiter:
    """Per-key token bucket — lightweight, in-memory (§16H.2)."""

    def __init__(self, rate_per_sec: float = 10, burst: int = 20) -> None:
        self.rate = rate_per_sec
        self.burst = burst
        self._buckets: dict[str, _Bucket] = {}
        self._lock = Lock()

    def allow(self, key: str, tokens: int = 1) -> bool:
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(tokens=float(self.burst), last_refill=now)
                self._buckets[key] = b
            # refill
            elapsed = now - b.last_refill
            b.tokens = min(self.burst, b.tokens + elapsed * self.rate)
            b.last_refill = now
            if b.tokens >= tokens:
                b.tokens -= tokens
                return True
            return False

    def retry_after(self, key: str, tokens: int = 1) -> float:
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                return 0.0
            need = tokens - b.tokens
            if need <= 0:
                return 0.0
            return need / self.rate if self.rate > 0 else 9999.0
