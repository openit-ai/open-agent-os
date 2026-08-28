"""Streaming engine — §16C.2

Async generator yielding events: text / tool / error / completion
Minimal, no hard deps. Can wrap an LLM call or emit mock events for offline.
Enhanced: OAOSContext propagation (tenant/agent/trace/vault)
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, asdict
from typing import Any, AsyncGenerator

EVENT_TYPES = frozenset({"text", "tool", "error", "completion", "progress"})


@dataclass
class StreamEvent:
    type: str  # text | tool | error | completion | progress
    data: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""
    session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # keep minimal
        out: dict[str, Any] = {"type": self.type, "data": self.data}
        if self.trace_id:
            out["trace_id"] = self.trace_id
        if self.session_id:
            out["session_id"] = self.session_id
        return out


class StreamingEngine:
    """Yield streaming events. Sync callers use stream(); async callers await.

    Example:
        engine = StreamingEngine()
        async for ev in engine.stream(prompt=\"hello\", session={\"session_id\":\"...\"}):
            print(ev)
    OAOSContext support: if session contains OAOSContext or headers, trace_id/session_id
    are propagated to every event so observability/audit stays correlated.
    """

    def __init__(self, chunk_delay: float = 0.02) -> None:
        self.chunk_delay = chunk_delay

    def _extract_ids(self, session: Any | None, oaos_context: Any | None = None) -> tuple[str, str]:
        """Derive (session_id, trace_id) from session dict/record or OAOSContext."""
        sid = ""
        tid = ""
        # Prefer explicit OAOSContext if provided
        if oaos_context is not None:
            if isinstance(oaos_context, dict):
                sid = str(oaos_context.get("session_id", "") or sid)
                tid = str(oaos_context.get("trace_id", "") or tid)
            else:
                sid = str(getattr(oaos_context, "session_id", "") or sid)
                tid = str(getattr(oaos_context, "trace_id", "") or tid)
        if isinstance(session, dict):
            sid = str(session.get("session_id", "") or sid)
            tid = str(session.get("trace_id", "") or tid)
        elif session is not None:
            sid = str(getattr(session, "session_id", "") or sid)
            tid = str(getattr(session, "trace_id", "") or tid)
        return sid, tid

    # ── core async generator ──
    async def stream(
        self,
        prompt: str = "",
        session: dict[str, Any] | Any | None = None,
        chunks: list[str] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        emit_completion: bool = True,
        oaos_context: Any | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield events for a prompt.  Offline mock if no LLM configured.

        Emits: text* -> tool* -> completion (or error on exception)
        oaos_context: optional OAOSContext to propagate trace/vault (pydantic-ai style)
        """
        sid, tid = self._extract_ids(session, oaos_context)
        try:
            # Optional: if litellm / httpx LLM is configured, stream from there.
            # Minimal impl: emit provided chunks or default mock.
            if chunks is None:
                # default mock — keeps tests/offline working
                chunks = ["Hello, ", "this is ", "a streaming response."]

            for ch in chunks:
                if self.chunk_delay:
                    await asyncio.sleep(self.chunk_delay)
                yield StreamEvent(type="text", data={"text": ch}, trace_id=tid, session_id=sid).to_dict()

            if tool_calls:
                for tc in tool_calls:
                    yield StreamEvent(type="tool", data={"tool": tc.get("tool", ""), "arguments": tc.get("arguments", {}), "result": tc.get("result")}, trace_id=tid, session_id=sid).to_dict()

            if emit_completion:
                yield StreamEvent(type="completion", data={"session_id": sid, "prompt": prompt}, trace_id=tid, session_id=sid).to_dict()
        except Exception as e:
            yield StreamEvent(type="error", data={"reason": str(e)}, trace_id=tid, session_id=sid).to_dict()
            if emit_completion:
                yield StreamEvent(type="completion", data={"session_id": sid, "error": str(e)}, trace_id=tid, session_id=sid).to_dict()

    # Alias expected by adapter contract
    def stream_events(self, session: Any, prompt: str = "", **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        return self.stream(prompt=prompt, session=session, **kwargs)

    # Helper to collect stream into list (useful for tests / non-streaming callers)
    async def collect(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        async for ev in self.stream(*args, **kwargs):  # type: ignore[misc]
            out.append(ev)
        return out

    # SSE formatting helper
    @staticmethod
    def to_sse(event: dict[str, Any]) -> str:
        return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# Module-level default engine
default_engine = StreamingEngine()


async def stream_text(prompt: str, session: dict[str, Any] | None = None, **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
    """Convenience function — yields from default engine."""
    async for ev in default_engine.stream(prompt=prompt, session=session, **kwargs):
        yield ev
