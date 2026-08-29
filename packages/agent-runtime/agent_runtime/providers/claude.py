"""Claude provider — via anthropic SDK, lazy import + mock fallback."""
from __future__ import annotations

import os
import time
import uuid
from typing import Any


def _is_mock_allowed() -> bool:
    # H7 immutable: prod always False, delegate to canonical gate when available
    try:
        from agent_runtime.env_gate import is_mock_allowed as _g
        return _g()
    except Exception:
        pass
    try:
        from execution_gateway.env_gate import is_mock_allowed as _g2  # type: ignore
        return _g2()
    except Exception:
        pass
    import os as _os
    # immutable prod gate — no OAOS_MOCK_FALLBACK bypass in production
    for k in ("OAOS_ENV","ENV","OAOS_ENVIRONMENT","APP_ENV","ENVIRONMENT"):
        if _os.getenv(k,"").strip().lower() in ("production","prod"):
            return False
    mf = _os.getenv("OAOS_MOCK_FALLBACK","").strip().lower()
    if mf in ("0","false","no","off"):
        return False
    return True
    if mf in ("0", "false", "no", "off"):
        return False
    if _os.getenv("OAOS_ENV", "").lower() in ("production", "prod"):
        return False
    return True


def _mock(model: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = str(m.get("content", ""))[:200]
            break
    return {
        "id": f"mock-claude-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"[mock:claude:{model}] echo: {last}" if last else f"[mock:claude:{model}] hello", "tool_calls": []}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

class ClaudeProvider:
    """Anthropic Claude via `anthropic` SDK. Config via env ANTHROPIC_API_KEY / api_key kwarg."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None, **_: Any) -> None:
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY") or os.getenv("OAOS_CLAUDE_API_KEY") or ""
        self.base_url = base_url or os.getenv("ANTHROPIC_BASE_URL") or os.getenv("CLAUDE_BASE_URL") or None
        self.default_model = model or os.getenv("CLAUDE_MODEL") or "claude-3-5-sonnet-20241022"

    async def call(self, messages: list[dict[str, Any]], model: str | None = None, tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> dict[str, Any]:
        resolved = model or self.default_model
        # If no API key or SDK missing -> mock
        try:
            import anthropic  # type: ignore
        except ImportError:
            if not _is_mock_allowed():
                raise RuntimeError("LLM provider unavailable: claude — mock fallback disabled in production")
            return _mock(resolved, messages, tools=tools)
        if not self.api_key:
            if not _is_mock_allowed():
                raise RuntimeError("LLM provider unavailable: claude — mock fallback disabled in production")
            return _mock(resolved, messages, tools=tools)
        # Translate messages: anthropic expects system separate
        system_parts: list[str] = []
        chat_messages: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                system_parts.append(str(content))
            elif role in ("user", "assistant"):
                # anthropic only allows user/assistant alternation; keep as is
                chat_messages.append({"role": role, "content": str(content)})
            elif role == "tool":
                # tool results -> user message with tool reference
                chat_messages.append({"role": "user", "content": f"[tool:{m.get('name','')}] {content}"})
        # Translate tools to anthropic format
        anth_tools: list[dict[str, Any]] | None = None
        if tools:
            anth_tools = []
            for t in tools:
                fn = t.get("function") or t
                name = fn.get("name", "")
                desc = fn.get("description", "")
                schema = fn.get("parameters") or {}
                anth_tools.append({"name": name, "description": desc, "input_schema": schema})
        try:
            # Prefer AsyncAnthropic
            if hasattr(anthropic, "AsyncAnthropic"):
                kwargs_client: dict[str, Any] = {"api_key": self.api_key}
                if self.base_url:
                    kwargs_client["base_url"] = self.base_url
                client = anthropic.AsyncAnthropic(**kwargs_client)  # type: ignore
                create_kwargs: dict[str, Any] = {"model": resolved, "max_tokens": kwargs.get("max_tokens", 1024), "messages": chat_messages or [{"role": "user", "content": ""}]}
                if system_parts:
                    create_kwargs["system"] = "\n".join(system_parts)
                if anth_tools:
                    create_kwargs["tools"] = anth_tools
                resp = await client.messages.create(**create_kwargs)  # type: ignore
                # Convert to OpenAI dict
                content_text = ""
                tool_calls: list[dict[str, Any]] = []
                try:
                    blocks = getattr(resp, "content", []) or []
                    for b in blocks:
                        if getattr(b, "type", "") == "text":
                            content_text += getattr(b, "text", "")
                        elif getattr(b, "type", "") == "tool_use":
                            tool_calls.append({"id": getattr(b, "id", f"call_{uuid.uuid4().hex[:8]}"), "type": "function", "function": {"name": getattr(b, "name", ""), "arguments": str(getattr(b, "input", {}))}})
                    # fallback if content is string
                    if not content_text and not tool_calls:
                        content_text = str(resp.content) if hasattr(resp, "content") else ""
                except Exception:
                    content_text = str(resp)
                finish = getattr(resp, "stop_reason", "stop") or "stop"
                # map anthropic stop_reason to openai finish_reason
                finish_map = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length"}
                finish_reason = finish_map.get(str(finish), "stop")
                return {
                    "id": getattr(resp, "id", f"claude-{uuid.uuid4().hex[:8]}"),
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": resolved,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content_text, "tool_calls": tool_calls}, "finish_reason": finish_reason}],
                    "usage": {"prompt_tokens": getattr(getattr(resp, "usage", None), "input_tokens", 0) if hasattr(resp, "usage") else 0, "completion_tokens": getattr(getattr(resp, "usage", None), "output_tokens", 0) if hasattr(resp, "usage") else 0, "total_tokens": 0},
                }
            else:
                if not _is_mock_allowed():
                    raise RuntimeError("LLM provider unavailable: claude — mock fallback disabled in production")
                return _mock(resolved, messages, tools=tools)
        except Exception:
            # On any API error, fallback to mock for offline/tests
            if not _is_mock_allowed():
                raise RuntimeError("LLM provider unavailable: claude — mock fallback disabled in production")
            return _mock(resolved, messages, tools=tools)
