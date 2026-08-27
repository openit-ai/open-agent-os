"""Observability — §16C.10

Structured events/spans, trace_id/session_id propagation, audit hook.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class RuntimeEvent:
    """Structured runtime event (§16C.10 required events)."""
    event_type: str  # session_start | model_request | model_response | tool_request | tool_result | retry | error | task_complete | task_cancel
    trace_id: str = ""
    session_id: str = ""
    request_id: str = ""
    agent_id: str = ""
    user_id: str = ""
    timestamp: str = field(default_factory=_now_iso)
    data: dict[str, Any] = field(default_factory=dict)
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # keep flat for easy audit
        return d

    @classmethod
    def from_session(cls, event_type: str, session: Any, **kwargs: Any) -> "RuntimeEvent":
        if isinstance(session, dict):
            return cls(
                event_type=event_type,
                trace_id=str(session.get("trace_id", "")),
                session_id=str(session.get("session_id", "")),
                request_id=str(session.get("request_id", kwargs.get("request_id", ""))),
                agent_id=str(session.get("agent_id", "")),
                user_id=str(session.get("user_id", "")),
                data=dict(kwargs.get("data") or {}),
            )
        return cls(
            event_type=event_type,
            trace_id=str(getattr(session, "trace_id", "")),
            session_id=str(getattr(session, "session_id", "")),
            request_id=str(getattr(session, "request_id", kwargs.get("request_id", ""))),
            agent_id=str(getattr(session, "agent_id", "")),
            user_id=str(getattr(session, "user_id", "")),
            data=dict(kwargs.get("data") or {}),
        )


@dataclass
class Span:
    trace_id: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: str | None = None
    name: str = ""
    start_ts: float = field(default_factory=time.time)
    end_ts: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"

    def end(self, status: str = "ok") -> None:
        self.end_ts = time.time()
        self.status = status

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.events.append({"name": name, "ts": time.time(), "attributes": attributes or {}})

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_ms": round((self.end_ts - self.start_ts) * 1000, 2) if self.end_ts else None,
            "attributes": self.attributes,
            "events": self.events,
            "status": self.status,
        }


class ObservabilityBus:
    """In-memory observability bus with audit hook."""

    def __init__(self):
        self.events: list[RuntimeEvent] = []
        self.spans: list[Span] = []
        self._audit_hooks: list[Callable[[RuntimeEvent], Any]] = []

    def add_audit_hook(self, hook: Callable[[RuntimeEvent], Any]) -> None:
        self._audit_hooks.append(hook)

    def emit(self, event: RuntimeEvent | dict[str, Any]) -> RuntimeEvent:
        if isinstance(event, dict):
            # normalize dict -> RuntimeEvent
            event = RuntimeEvent(
                event_type=str(event.get("event_type") or event.get("type") or "unknown"),
                trace_id=str(event.get("trace_id", "")),
                session_id=str(event.get("session_id", "")),
                request_id=str(event.get("request_id", "")),
                agent_id=str(event.get("agent_id", "")),
                user_id=str(event.get("user_id", "")),
                data=dict(event.get("data") or {k: v for k, v in event.items() if k not in ("event_type", "type", "trace_id", "session_id", "request_id", "agent_id", "user_id")}),
            )
        self.events.append(event)
        for hook in self._audit_hooks:
            try:
                hook(event)
            except Exception:
                pass  # audit hook must not break emit
        return event

    def start_span(self, trace_id: str, name: str, parent_span_id: str | None = None, attributes: dict[str, Any] | None = None) -> Span:
        span = Span(trace_id=trace_id, name=name, parent_span_id=parent_span_id, attributes=attributes or {})
        self.spans.append(span)
        return span

    def query_events(self, trace_id: str | None = None, session_id: str | None = None, event_type: str | None = None) -> list[RuntimeEvent]:
        out = self.events
        if trace_id:
            out = [e for e in out if e.trace_id == trace_id]
        if session_id:
            out = [e for e in out if e.session_id == session_id]
        if event_type:
            out = [e for e in out if e.event_type == event_type]
        return out


# module-level bus
default_bus = ObservabilityBus()
