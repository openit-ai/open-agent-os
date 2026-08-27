"""Quality metrics — §16D.4

Collects latency, success_rate, policy_deny_rate, audit events
and exposes them in Prometheus text exposition format.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class LatencySample:
    value_ms: float
    ts: float = field(default_factory=time.time)


class MetricsCollector:
    """In-memory metrics collector with Prometheus exposition.

    Thread-safe, no external deps. Intended to be wrapped by a FastAPI
    ``/metrics`` endpoint in the execution gateway / control-plane.
    """

    def __init__(self, max_latency_samples: int = 10000) -> None:
        self._lock = Lock()
        self._request_count: int = 0
        self._success_count: int = 0
        self._failure_count: int = 0
        self._policy_deny_count: int = 0
        self._audit_event_count: int = 0
        self._latencies: deque[LatencySample] = deque(maxlen=max_latency_samples)
        self._tool_calls: Counter[str] = Counter()
        self._tool_failures: Counter[str] = Counter()
        self._status_codes: Counter[int] = Counter()
        self._start_time = time.time()

    # -- recording ---------------------------------------------------

    def observe_latency(self, latency_ms: float, *, tool: str | None = None, status: int | None = None) -> None:
        with self._lock:
            self._request_count += 1
            self._latencies.append(LatencySample(latency_ms))
            if tool:
                self._tool_calls[tool] += 1
            if status is not None:
                self._status_codes[status] += 1

    def record_success(self, latency_ms: float | None = None, *, tool: str | None = None) -> None:
        with self._lock:
            self._success_count += 1
            self._request_count += 1
            if latency_ms is not None:
                self._latencies.append(LatencySample(latency_ms))
            if tool:
                self._tool_calls[tool] += 1

    def record_failure(self, latency_ms: float | None = None, *, tool: str | None = None) -> None:
        with self._lock:
            self._failure_count += 1
            self._request_count += 1
            if latency_ms is not None:
                self._latencies.append(LatencySample(latency_ms))
            if tool:
                self._tool_failures[tool] += 1
                self._tool_calls[tool] += 1

    def record_policy_deny(self) -> None:
        with self._lock:
            self._policy_deny_count += 1
            self._request_count += 1
            self._failure_count += 1

    def record_audit_event(self, event_type: str = "generic") -> None:
        with self._lock:
            self._audit_event_count += 1

    # -- computed ----------------------------------------------------

    def success_rate(self) -> float:
        with self._lock:
            total = self._success_count + self._failure_count
            if total == 0:
                return 1.0
            return self._success_count / total

    def policy_deny_rate(self) -> float:
        with self._lock:
            if self._request_count == 0:
                return 0.0
            return self._policy_deny_count / self._request_count

    def latency_percentile(self, p: float) -> float | None:
        """p in 0..100. Returns None if no samples."""
        with self._lock:
            if not self._latencies:
                return None
            vals = sorted(s.value_ms for s in self._latencies)
        # nearest-rank
        import math

        k = math.ceil(p / 100 * len(vals)) - 1
        k = max(0, min(k, len(vals) - 1))
        return vals[k]

    def latency_avg(self) -> float | None:
        with self._lock:
            if not self._latencies:
                return None
            return sum(s.value_ms for s in self._latencies) / len(self._latencies)

    def snapshot(self) -> dict:
        with self._lock:
            total = self._success_count + self._failure_count
            s_rate = (self._success_count / total) if total else 1.0
            deny_rate = (self._policy_deny_count / self._request_count) if self._request_count else 0.0
            lat_vals = sorted(s.value_ms for s in self._latencies) if self._latencies else []
            import math

            def pct(p: float) -> float | None:
                if not lat_vals:
                    return None
                k = math.ceil(p / 100 * len(lat_vals)) - 1
                k = max(0, min(k, len(lat_vals) - 1))
                return lat_vals[k]

            return {
                "request_count": self._request_count,
                "success_count": self._success_count,
                "failure_count": self._failure_count,
                "policy_deny_count": self._policy_deny_count,
                "audit_event_count": self._audit_event_count,
                "success_rate": round(s_rate, 4),
                "policy_deny_rate": round(deny_rate, 4),
                "latency_avg_ms": round(sum(lat_vals) / len(lat_vals), 2) if lat_vals else None,
                "latency_p50_ms": pct(50),
                "latency_p95_ms": pct(95),
                "latency_p99_ms": pct(99),
                "uptime_seconds": round(time.time() - self._start_time, 1),
                "tool_calls": dict(self._tool_calls),
                "tool_failures": dict(self._tool_failures),
            }

    # -- Prometheus exposition ----------------------------------------

    def to_prometheus(self) -> str:
        s = self.snapshot()
        lines: list[str] = []
        lines.append("# HELP oaos_requests_total Total requests observed")
        lines.append("# TYPE oaos_requests_total counter")
        lines.append(f"oaos_requests_total {s['request_count']}")
        lines.append("# HELP oaos_success_total Successful requests")
        lines.append("# TYPE oaos_success_total counter")
        lines.append(f"oaos_success_total {s['success_count']}")
        lines.append("# HELP oaos_failure_total Failed requests")
        lines.append("# TYPE oaos_failure_total counter")
        lines.append(f"oaos_failure_total {s['failure_count']}")
        lines.append("# HELP oaos_policy_deny_total Policy deny count")
        lines.append("# TYPE oaos_policy_deny_total counter")
        lines.append(f"oaos_policy_deny_total {s['policy_deny_count']}")
        lines.append("# HELP oaos_policy_deny_rate Policy deny rate 0..1")
        lines.append("# TYPE oaos_policy_deny_rate gauge")
        lines.append(f"oaos_policy_deny_rate {s['policy_deny_rate']}")
        lines.append("# HELP oaos_success_rate Success rate 0..1")
        lines.append("# TYPE oaos_success_rate gauge")
        lines.append(f"oaos_success_rate {s['success_rate']}")
        lines.append("# HELP oaos_audit_events_total Audit events emitted")
        lines.append("# TYPE oaos_audit_events_total counter")
        lines.append(f"oaos_audit_events_total {s['audit_event_count']}")
        if s["latency_avg_ms"] is not None:
            lines.append("# HELP oaos_latency_avg_ms Average latency ms")
            lines.append("# TYPE oaos_latency_avg_ms gauge")
            lines.append(f"oaos_latency_avg_ms {s['latency_avg_ms']}")
        for label, val in [("p50", s["latency_p50_ms"]), ("p95", s["latency_p95_ms"]), ("p99", s["latency_p99_ms"])]:
            if val is not None:
                lines.append(f'oaos_latency_ms{{quantile="{label}"}} {val}')
        # per-tool counters
        for tool, cnt in s["tool_calls"].items():
            safe = tool.replace('"', "_")
            lines.append(f'oaos_tool_calls_total{{tool="{safe}"}} {cnt}')
        for tool, cnt in s["tool_failures"].items():
            safe = tool.replace('"', "_")
            lines.append(f'oaos_tool_failures_total{{tool="{safe}"}} {cnt}')
        lines.append("")
        return "\n".join(lines)

    def reset(self) -> None:
        with self._lock:
            self._request_count = 0
            self._success_count = 0
            self._failure_count = 0
            self._policy_deny_count = 0
            self._audit_event_count = 0
            self._latencies.clear()
            self._tool_calls.clear()
            self._tool_failures.clear()
            self._status_codes.clear()
            self._start_time = time.time()


# global singleton used by gateway
default_metrics = MetricsCollector()
