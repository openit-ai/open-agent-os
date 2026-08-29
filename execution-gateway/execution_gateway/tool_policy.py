"""Execution Gateway Tool Policy — §16H v1.4.1

Capability 단독으로는 위험한 argument/대량 호출을 막지 못함.
Tool-level 정책: argument validation + field scope + row/file limits + rate limit + bulk protection.

Deterministic, never LLM-based (§45).

Distributed/operational boundary:
- Rate/quota state is Redis-primary when REDIS_URL is set (distributed, shared across
  replicas). In-memory token bucket is retained as explicit test fallback ONLY in non-prod
  (OAOS_ENV != production or OAOS_ALLOW_TEST_FALLBACK=1).
- Production (OAOS_ENV=production) fail-closed: if REDIS_URL is set, Redis must be
  reachable; otherwise rate limit checks raise RuntimeError (deny). If no REDIS_URL in
  prod, we raise as well (distributed state required). This prevents a single replica
  from bypassing limits.
- Limitations: in-memory fallback is process-local, not shared across replicas and does not
  survive restart — unsuitable for HA. Prod MUST configure REDIS_URL.
"""
from __future__ import annotations

import time
import os
import logging
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

# ── helpers: env / redis ─────────────────────────────────────────────────

def _is_prod() -> bool:
    return os.environ.get("OAOS_ENV", "").lower() in ("production", "prod")


def _redis_url() -> str | None:
    u = os.environ.get("REDIS_URL") or os.environ.get("OAOS_REDIS_URL")
    return u.strip() if u and u.strip() else None


def _allow_in_memory_fallback() -> bool:
    if _is_prod():
        return os.environ.get("OAOS_ALLOW_TEST_FALLBACK", "").lower() in ("1", "true", "yes")
    return True


def _redis_client():
    url = _redis_url()
    if not url:
        return None
    try:
        import redis  # type: ignore

        return redis.Redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
    except Exception as e:
        if _is_prod():
            raise RuntimeError(f"ToolRateLimiter: redis client unavailable in production: {e}") from e
        logger.debug("ToolRateLimiter redis unavailable (non-prod fallback): %s", e)
        return None


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
    if policy and policy.allowed_actions:
        allowed = {a.upper() for a in policy.allowed_actions}
        if action.upper() not in allowed:
            return False, f"action {action} not allowed for {policy.tool} (allowed: {policy.allowed_actions})"
    if policy and policy.denied_fields:
        denied = {f.lower() for f in policy.denied_fields}
        for k in args.keys():
            if k.lower() in denied:
                return False, f"denied field: {k}"
        for f in args.get("fields", []) if isinstance(args.get("fields"), list) else []:
            if str(f).lower() in denied:
                return False, f"denied field in fields: {f}"
    if policy and policy.allowed_fields:
        allowed_f = {f.lower() for f in policy.allowed_fields}
        for f in args.get("fields", []) if isinstance(args.get("fields"), list) else []:
            if str(f).lower() not in allowed_f:
                return False, f"field not allowed: {f} (allowed: {policy.allowed_fields})"
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
    """Per-key token bucket — lightweight, in-memory + optional Redis primary (§16H.2).

    Production: Redis is primary (distributed). In-memory fallback ONLY in non-prod.
    Fail-closed in prod if Redis configured but unreachable.
    Limitations: in-memory is process-local, not distributed.
    """

    def __init__(self, rate_per_sec: float = 10, burst: int = 20) -> None:
        self.rate = rate_per_sec
        self.burst = burst
        self._buckets: dict[str, _Bucket] = {}
        self._lock = Lock()

    def _redis_allow(self, key: str, tokens: int = 1) -> bool | None:
        """Try Redis path; return None if no Redis / fallback to memory."""
        r = _redis_client()
        if r is None:
            return None
        rk = f"oaos:ratelimit:{key}"
        # Use simple fixed-window-ish token bucket via Redis INCR + TTL
        # To keep deterministic semantics, we emulate token bucket with 2 keys: tokens + timestamp
        # Simplified: use Redis sorted set or INCR with window = burst/rate
        # For MVP hardening, use INCR with window = 1s and allow burst as max per window
        try:
            # Lua: token bucket via Redis
            # We store tokens and last_refill as hash fields
            now = time.time()
            # Try to use hash; fallback to simple incr if not supported
            pipe = r.pipeline()
            pipe.hgetall(rk)
            vals = pipe.execute()[0] if False else None
        except Exception:
            pass
        # Fallback implementation: use Redis with atomic Lua for true token bucket
        # To avoid complexity, use INCR over 1-second window as approximation for prod hardening
        # But to preserve burst semantics, implement via Lua script
        try:
            lua = """
            local rk = KEYS[1]
            local rate = tonumber(ARGV[1])
            local burst = tonumber(ARGV[2])
            local need = tonumber(ARGV[3])
            local now = tonumber(ARGV[4])
            local data = redis.call('HMGET', rk, 'tokens', 'last_refill')
            local tokens = tonumber(data[1])
            local last = tonumber(data[2])
            if tokens == nil then tokens = burst end
            if last == nil then last = now end
            local elapsed = now - last
            tokens = math.min(burst, tokens + elapsed * rate)
            last = now
            local allowed = 0
            if tokens >= need then
                tokens = tokens - need
                allowed = 1
            end
            redis.call('HMSET', rk, 'tokens', tokens, 'last_refill', last)
            redis.call('EXPIRE', rk, 60)
            return allowed
            """
            res = r.eval(lua, 1, rk, self.rate, self.burst, tokens, time.time())
            return bool(int(res))
        except Exception as e:
            if _is_prod():
                raise RuntimeError(f"ToolRateLimiter Redis unavailable in production: {e}") from e
            logger.debug("ToolRateLimiter Redis allow fallback to memory: %s", e)
            return None

    def _redis_retry_after(self, key: str, tokens: int = 1) -> float | None:
        r = _redis_client()
        if r is None:
            return None
        try:
            rk = f"oaos:ratelimit:{key}"
            data = r.hmget(rk, "tokens", "last_refill")
            if not data or data[0] is None:
                return 0.0
            tokens_avail = float(data[0])
            need = tokens - tokens_avail
            if need <= 0:
                return 0.0
            return need / self.rate if self.rate > 0 else 9999.0
        except Exception as e:
            if _is_prod():
                raise RuntimeError(f"ToolRateLimiter Redis retry_after unavailable in production: {e}") from e
            return None

    def allow(self, key: str, tokens: int = 1) -> bool:
        # Production fail-closed: if REDIS_URL configured, must use Redis
        redis_res = self._redis_allow(key, tokens)
        if redis_res is not None:
            return redis_res
        if _is_prod() and _redis_url():
            # Redis was configured but _redis_allow returned None due to error (non-prod path would fallback, prod raises above)
            raise RuntimeError("ToolRateLimiter: Redis required in production but unavailable (fail-closed)")
        if _is_prod() and not _allow_in_memory_fallback():
            raise RuntimeError("ToolRateLimiter: in-memory fallback not allowed in production (fail-closed)")
        # In-memory fallback (non-prod / tests)
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(tokens=float(self.burst), last_refill=now)
                self._buckets[key] = b
            elapsed = now - b.last_refill
            b.tokens = min(self.burst, b.tokens + elapsed * self.rate)
            b.last_refill = now
            if b.tokens >= tokens:
                b.tokens -= tokens
                return True
            return False

    def retry_after(self, key: str, tokens: int = 1) -> float:
        redis_res = self._redis_retry_after(key, tokens)
        if redis_res is not None:
            return redis_res
        if _is_prod() and _redis_url():
            raise RuntimeError("ToolRateLimiter: Redis required in production but unavailable (fail-closed)")
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                return 0.0
            need = tokens - b.tokens
            if need <= 0:
                return 0.0
            return need / self.rate if self.rate > 0 else 9999.0


# ── Mattermost colleague DM policy (§14 §16H) ───────────────────────
MATTERMOST_COLLEAGUE_DM_POLICY = ToolPolicy(
    tool="notify_colleague",
    allowed_actions=["SEND"],
    limits={"max_results": 50, "rate_per_sec": 5, "burst": 20, "max_text_length": 4000},
)

MATTERMOST_SEND_DM_POLICY = ToolPolicy(
    tool="mattermost_send_direct_message",
    allowed_actions=["SEND"],
    limits={"max_results": 50, "rate_per_sec": 5, "burst": 20, "max_text_length": 4000},
)

TOOL_POLICIES: dict[str, ToolPolicy] = {
    "notify_colleague": MATTERMOST_COLLEAGUE_DM_POLICY,
    "mattermost_send_direct_message": MATTERMOST_SEND_DM_POLICY,
    "mattermost_send_dm": MATTERMOST_SEND_DM_POLICY,
}

_COLLEAGUE_RATE_LIMITER: ToolRateLimiter | None = None

def get_colleague_rate_limiter() -> ToolRateLimiter:
    global _COLLEAGUE_RATE_LIMITER
    if _COLLEAGUE_RATE_LIMITER is None:
        _COLLEAGUE_RATE_LIMITER = ToolRateLimiter(rate_per_sec=5, burst=20)
    return _COLLEAGUE_RATE_LIMITER

def get_tool_policy(tool_name: str) -> ToolPolicy | None:
    """Lookup ToolPolicy for a tool — returns None if no specific policy."""
    return TOOL_POLICIES.get(tool_name)

def is_approval_required_for_tool(tool_name: str) -> bool:
    """Whether tool requires approval. Colleague DM is approval_not_required (but audit logged)."""
    if tool_name in TOOL_POLICIES:
        return False
    return True  # default: defer to Risk/PolicyEngine
