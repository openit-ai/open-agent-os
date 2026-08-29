"""Codex provider — via openai SDK (Codex uses OpenAI-compatible API)."""
from __future__ import annotations

import os
import time
import uuid
import json
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
        "id": f"mock-codex-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"[mock:codex:{model}] echo: {last}" if last else f"[mock:codex:{model}] hello", "tool_calls": []}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

class CodexProvider:
    """OpenAI-compatible Codex provider. Env: OPENAI_API_KEY / CODEX_API_KEY."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None, **_: Any) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("CODEX_API_KEY") or os.getenv("OAOS_CODEX_API_KEY") or ""
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL") or os.getenv("CODEX_BASE_URL") or os.getenv("OAOS_CODEX_BASE_URL") or None
        self.default_model = model or os.getenv("CODEX_MODEL") or "gpt-4o-mini"

    async def call(self, messages: list[dict[str, Any]], model: str | None = None, tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> dict[str, Any]:
        resolved = model or self.default_model
        try:
            import openai  # type: ignore
        except ImportError:
            if not _is_mock_allowed():
                raise RuntimeError("LLM provider unavailable: codex — mock fallback disabled in production")
            return _mock(resolved, messages, tools=tools)
        if not self.api_key:
            if not _is_mock_allowed():
                raise RuntimeError("LLM provider unavailable: codex — mock fallback disabled in production")
            return _mock(resolved, messages, tools=tools)
        try:
            client_kwargs: dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                client_kwargs["base_url"] = self.base_url
            client = openai.AsyncOpenAI(**client_kwargs)  # type: ignore
            ckwargs: dict[str, Any] = {}
            if tools:
                ckwargs["tools"] = tools
                ckwargs["tool_choice"] = kwargs.get("tool_choice", "auto")
            # Filter unexpected kwargs for openai
            for k in ("temperature", "max_tokens", "top_p"):
                if k in kwargs:
                    ckwargs[k] = kwargs[k]
            resp = await client.chat.completions.create(model=resolved, messages=messages, **ckwargs)  # type: ignore
            # Serialize to dict (openai pydantic model)
            try:
                data = resp.model_dump()  # type: ignore
            except Exception:
                try:
                    data = dict(resp)  # type: ignore
                except Exception:
                    data = {"choices": [{"message": {"role": "assistant", "content": str(resp)}, "finish_reason": "stop"}], "model": resolved, "id": f"codex-{uuid.uuid4().hex[:8]}"}
            # Normalize to our shape if needed
            if "choices" not in data:
                if not _is_mock_allowed():
                    raise RuntimeError("LLM provider unavailable: codex — mock fallback disabled in production")
                data = _mock(resolved, messages, tools=tools)
            # Ensure required fields
            data.setdefault("object", "chat.completion")
            data.setdefault("created", int(time.time()))
            data.setdefault("model", resolved)
            return data  # type: ignore
        except Exception:
            if not _is_mock_allowed():
                raise RuntimeError("LLM provider unavailable: codex — mock fallback disabled in production")
            return _mock(resolved, messages, tools=tools)
