"""Context window management — §16C.8

Token-aware window with compression hook, conversation history, session resume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
import inspect


def _estimate_tokens(text: str) -> int:
    # rough: 1 token ~ 4 chars, fallback when tokenizer unavailable
    return max(1, len(text) // 4)


def _msg_tokens(msg: dict[str, Any]) -> int:
    content = str(msg.get("content", ""))
    # role overhead
    return _estimate_tokens(content) + 4


@dataclass
class ContextWindow:
    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    max_tokens: int = 128_000
    compression_hook: Callable[[list[dict[str, Any]]], Any] | None = None
    _total_tokens: int | None = None

    def token_count(self) -> int:
        return sum(_msg_tokens(m) for m in self.messages)

    def usage(self) -> dict[str, Any]:
        total = self.token_count()
        return {
            "session_id": self.session_id,
            "messages": len(self.messages),
            "tokens": total,
            "max_tokens": self.max_tokens,
            "utilization": round(total / self.max_tokens, 4) if self.max_tokens else 0,
            "needs_compaction": total > int(self.max_tokens * 0.85),
        }

    def append(self, message: dict[str, Any] | list[dict[str, Any]]) -> None:
        if isinstance(message, list):
            self.messages.extend(message)
        else:
            self.messages.append(message)

    def get(self, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is None:
            return list(self.messages)
        return list(self.messages[-limit:])

    async def compact(self, max_tokens: int | None = None) -> dict[str, Any]:
        """Compact if over budget. Uses hook if provided, else drops oldest non-system."""
        target = max_tokens if max_tokens is not None else self.max_tokens
        before = self.token_count()
        if before <= target:
            return {"compacted": False, "before_tokens": before, "after_tokens": before}

        if self.compression_hook is not None:
            res = self.compression_hook(self.messages)
            if inspect.isawaitable(res):
                res = await res  # type: ignore
            if isinstance(res, list):
                self.messages = res
            elif isinstance(res, dict) and "messages" in res:
                self.messages = list(res["messages"])
            after = self.token_count()
            return {"compacted": True, "before_tokens": before, "after_tokens": after, "hook": True}

        # default: keep system + last N that fit
        system_msgs = [m for m in self.messages if m.get("role") == "system"]
        other = [m for m in self.messages if m.get("role") != "system"]
        # estimate budget for system
        sys_tokens = sum(_msg_tokens(m) for m in system_msgs)
        budget = target - sys_tokens
        # keep tail that fits
        kept: list[dict[str, Any]] = []
        tokens = 0
        for m in reversed(other):
            t = _msg_tokens(m)
            if tokens + t > budget:
                break
            kept.append(m)
            tokens += t
        kept.reverse()
        self.messages = system_msgs + kept
        after = self.token_count()
        return {"compacted": True, "before_tokens": before, "after_tokens": after, "hook": False}


class ContextManager:
    """Manages per-session context windows."""

    def __init__(self, default_max_tokens: int = 128_000):
        self.default_max_tokens = default_max_tokens
        self._windows: dict[str, ContextWindow] = {}

    def get_or_create(self, session_id: str, max_tokens: int | None = None, compression_hook: Callable | None = None) -> ContextWindow:
        if session_id not in self._windows:
            self._windows[session_id] = ContextWindow(
                session_id=session_id,
                max_tokens=max_tokens or self.default_max_tokens,
                compression_hook=compression_hook,
            )
        return self._windows[session_id]

    def get(self, session_id: str) -> ContextWindow | None:
        return self._windows.get(session_id)

    def update(self, session_id: str, messages: list[dict[str, Any]]) -> ContextWindow:
        w = self.get_or_create(session_id)
        w.append(messages)
        return w

    def set_compression_hook(self, session_id: str, hook: Callable) -> None:
        w = self.get_or_create(session_id)
        w.compression_hook = hook  # type: ignore


# module-level manager
default_manager = ContextManager()
