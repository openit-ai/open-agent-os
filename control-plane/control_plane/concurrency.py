"""Concurrency control — §16D.3

- Semaphore per tenant / per user
- Token-bucket rate limiter
- Bounded queue with backpressure (429)
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock, Semaphore


class ConcurrencyLimitExceeded(RuntimeError):
    """Raised when concurrency or queue limits are exceeded (maps to HTTP 429)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# ── Per-key semaphore manager ──────────────────────────────────────

class KeyedSemaphore:
    """Manages per-key counting semaphores (sync + async friendly).

    Each key (tenant_id / user_id) gets its own semaphore with *limit*
    concurrent slots.  Keys are created lazily.
    """

    def __init__(self, default_limit: int) -> None:
        self.default_limit = default_limit
        self._limits: dict[str, int] = {}
        self._semaphores: dict[str, Semaphore] = {}
        self._lock = Lock()

    def set_limit(self, key: str, limit: int) -> None:
        with self._lock:
            self._limits[key] = limit
            # recreate semaphore if limit changed (best-effort)
            if key in self._semaphores:
                del self._semaphores[key]

    def _get(self, key: str) -> Semaphore:
        with self._lock:
            if key not in self._semaphores:
                lim = self._limits.get(key, self.default_limit)
                self._semaphores[key] = Semaphore(lim)
            return self._semaphores[key]

    def acquire(self, key: str, timeout: float | None = None) -> bool:
        sem = self._get(key)
        if timeout is None:
            return sem.acquire(blocking=False)
        return sem.acquire(timeout=timeout)

    def release(self, key: str) -> None:
        sem = self._get(key)
        try:
            sem.release()
        except ValueError:
            pass  # over-release guard

    def available(self, key: str) -> int:
        # not precise but useful for metrics
        sem = self._get(key)
        # Semaphore doesn't expose count; we approximate via private _value
        try:
            return sem._value  # type: ignore[attr-defined]
        except Exception:
            return -1

    # async helpers
    async def acquire_async(self, key: str, timeout: float | None = 0) -> bool:
        # run sync acquire in thread to avoid blocking event loop for long timeouts
        # but for non-blocking we just check immediately
        if timeout is None or timeout == 0:
            return self.acquire(key, timeout=0)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.acquire(key, timeout=timeout))

    async def release_async(self, key: str) -> None:
        self.release(key)


# ── Token bucket rate limiter ──────────────────────────────────────

@dataclass
class TokenBucket:
    rate_per_sec: float  # tokens added per second
    burst: int  # max bucket size
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.burst)
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate_per_sec)
        self._last_refill = now

    def consume(self, tokens: int = 1) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def retry_after(self, tokens: int = 1) -> float:
        with self._lock:
            self._refill()
            deficit = tokens - self._tokens
            if deficit <= 0:
                return 0.0
            return deficit / self.rate_per_sec if self.rate_per_sec > 0 else 0.0

    @property
    def available_tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens


class RateLimiter:
    """Per-key token-bucket rate limiter."""

    def __init__(self, rate_per_sec: float = 10, burst: int = 20) -> None:
        self.rate_per_sec = rate_per_sec
        self.burst = burst
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = Lock()

    def _bucket(self, key: str) -> TokenBucket:
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = TokenBucket(self.rate_per_sec, self.burst)
            return self._buckets[key]

    def allow(self, key: str, tokens: int = 1) -> bool:
        return self._bucket(key).consume(tokens)

    def retry_after(self, key: str, tokens: int = 1) -> float:
        return self._bucket(key).retry_after(tokens)


# ── Bounded queue ──────────────────────────────────────────────────

class BoundedQueue:
    """FIFO queue with fixed capacity; push raises 429 when full."""

    def __init__(self, capacity: int = 100) -> None:
        self.capacity = capacity
        self._q: deque = deque()
        self._lock = Lock()

    def push(self, item) -> None:
        with self._lock:
            if len(self._q) >= self.capacity:
                raise ConcurrencyLimitExceeded(
                    f"queue full ({self.capacity}) — backpressure",
                    retry_after=1.0,
                )
            self._q.append(item)

    def pop(self):
        with self._lock:
            if not self._q:
                return None
            return self._q.popleft()

    def __len__(self) -> int:
        with self._lock:
            return len(self._q)

    @property
    def depth(self) -> int:
        return len(self)

    def is_full(self) -> bool:
        return len(self) >= self.capacity


# ── Unified concurrency controller (§16D.3) ─────────────────────────

@dataclass
class ConcurrencyConfig:
    tenant_concurrency: int = 10  # max concurrent sessions per tenant
    user_concurrency: int = 3  # max concurrent per user
    rate_per_sec: float = 10
    rate_burst: int = 20
    queue_capacity: int = 100


class ConcurrencyController:
    """Combines semaphore (tenant/user), rate limit, and queue.

    Typical usage (sync):

        ctrl = ConcurrencyController(ConcurrencyConfig(...))
        ctrl.acquire("tenant-a", "employee:kim")  # raises ConcurrencyLimitExceeded on backpressure
        try:
            do_work()
        finally:
            ctrl.release("tenant-a", "employee:kim")

    For async code use ``acquire_async`` / ``release_async``.
    """

    def __init__(self, config: ConcurrencyConfig | None = None) -> None:
        self.config = config or ConcurrencyConfig()
        self.tenant_sem = KeyedSemaphore(self.config.tenant_concurrency)
        self.user_sem = KeyedSemaphore(self.config.user_concurrency)
        self.rate_limiter = RateLimiter(self.config.rate_per_sec, self.config.rate_burst)
        self.queue = BoundedQueue(self.config.queue_capacity)
        self._active: dict[str, int] = {}  # for metrics
        self._lock = Lock()

    # -- sync acquire ------------------------------------------------

    def acquire(self, tenant_id: str, user_id: str) -> None:
        # 1. rate limit
        key = f"{tenant_id}:{user_id}"
        if not self.rate_limiter.allow(key):
            retry = self.rate_limiter.retry_after(key)
            raise ConcurrencyLimitExceeded(f"rate limit exceeded for {key}", retry_after=retry)
        # 2. tenant semaphore (non-blocking → queue or 429)
        if not self.tenant_sem.acquire(tenant_id, timeout=0):
            # try queue
            try:
                self.queue.push({"tenant": tenant_id, "user": user_id, "ts": time.time()})
            except ConcurrencyLimitExceeded:
                raise ConcurrencyLimitExceeded(
                    f"tenant concurrency exceeded for {tenant_id} and queue full", retry_after=1.0
                )
            raise ConcurrencyLimitExceeded(
                f"tenant concurrency exceeded for {tenant_id} — queued", retry_after=0.5
            )
        # 3. user semaphore
        if not self.user_sem.acquire(user_id, timeout=0):
            self.tenant_sem.release(tenant_id)
            raise ConcurrencyLimitExceeded(f"user concurrency exceeded for {user_id}", retry_after=0.5)
        with self._lock:
            self._active[key] = self._active.get(key, 0) + 1

    def release(self, tenant_id: str, user_id: str) -> None:
        self.tenant_sem.release(tenant_id)
        self.user_sem.release(user_id)
        key = f"{tenant_id}:{user_id}"
        with self._lock:
            if key in self._active:
                self._active[key] = max(0, self._active[key] - 1)
                if self._active[key] == 0:
                    del self._active[key]
        # drain one from queue if present (caller can poll queue separately)
        # we don't auto-admit queued items here to keep semantics simple

    # -- async variants ----------------------------------------------

    async def acquire_async(self, tenant_id: str, user_id: str) -> None:
        # reuse sync logic but via async semaphore acquire for tenant/user if needed
        key = f"{tenant_id}:{user_id}"
        if not self.rate_limiter.allow(key):
            retry = self.rate_limiter.retry_after(key)
            raise ConcurrencyLimitExceeded(f"rate limit exceeded for {key}", retry_after=retry)
        ok = await self.tenant_sem.acquire_async(tenant_id, timeout=0)
        if not ok:
            try:
                self.queue.push({"tenant": tenant_id, "user": user_id, "ts": time.time()})
            except ConcurrencyLimitExceeded:
                raise ConcurrencyLimitExceeded(
                    f"tenant concurrency exceeded for {tenant_id} and queue full", retry_after=1.0
                )
            raise ConcurrencyLimitExceeded(
                f"tenant concurrency exceeded for {tenant_id} — queued", retry_after=0.5
            )
        ok2 = await self.user_sem.acquire_async(user_id, timeout=0)
        if not ok2:
            await self.tenant_sem.release_async(tenant_id)
            raise ConcurrencyLimitExceeded(f"user concurrency exceeded for {user_id}", retry_after=0.5)
        with self._lock:
            self._active[key] = self._active.get(key, 0) + 1

    async def release_async(self, tenant_id: str, user_id: str) -> None:
        self.release(tenant_id, user_id)

    # -- metrics / inspection ----------------------------------------

    def stats(self) -> dict:
        with self._lock:
            active = dict(self._active)
        return {
            "active_sessions": sum(active.values()),
            "active_by_key": active,
            "queue_depth": self.queue.depth,
            "queue_capacity": self.queue.capacity,
        }
