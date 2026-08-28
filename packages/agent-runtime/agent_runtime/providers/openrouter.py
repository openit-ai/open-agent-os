"""OpenRouter provider — aggregated gateway via OpenAI-compatible API (https://openrouter.ai)."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any


def _is_mock_allowed() -> bool:
    import os as _os

    mf = _os.getenv("OAOS_MOCK_FALLBACK", "").lower()
    if mf in ("1", "true", "yes", "on"):
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
        "id": f"mock-openrouter-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"[mock:openrouter:{model}] echo: {last}" if last else f"[mock:openrouter:{model}] hello",
                    "tool_calls": [],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class OpenRouterProvider:
    """OpenRouter via OpenAI-compat API. Env: OPENROUTER_API_KEY / api_key kwarg. Default: https://openrouter.ai/api/v1."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None, **_: Any) -> None:
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY") or os.getenv("OAOS_OPENROUTER_API_KEY") or ""
        self.base_url = (base_url or os.getenv("OPENROUTER_BASE_URL") or os.getenv("OAOS_OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
        self.default_model = model or os.getenv("OPENROUTER_MODEL") or "openrouter/auto"

    async def call(self, messages: list[dict[str, Any]], model: str | None = None, tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> dict[str, Any]:
        resolved = model or self.default_model
        if not self.api_key:
            if not _is_mock_allowed():
                raise RuntimeError("LLM provider unavailable: openrouter — mock fallback disabled in production")
            return _mock(resolved, messages, tools=tools)

        # Prefer openai SDK if available (parity with CodexProvider), else raw httpx
        try:
            import openai  # type: ignore

            has_openai = True
        except ImportError:
            has_openai = False

        if has_openai:
            try:
                client = openai.AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)  # type: ignore
                ckwargs: dict[str, Any] = {}
                if tools:
                    ckwargs["tools"] = tools
                    ckwargs["tool_choice"] = kwargs.get("tool_choice", "auto")
                for k in ("temperature", "max_tokens", "top_p", "stream"):
                    if k in kwargs:
                        ckwargs[k] = kwargs[k]
                # OpenRouter supports extra_headers for ranking; pass referer/title via default headers
                extra_headers = {"HTTP-Referer": "https://github.com/openit-ai/open-agent-os", "X-Title": "Open Agent OS"}
                ckwargs["extra_headers"] = extra_headers
                resp = await client.chat.completions.create(model=resolved, messages=messages, **ckwargs)  # type: ignore
                try:
                    data = resp.model_dump()  # type: ignore
                except Exception:
                    try:
                        data = dict(resp)  # type: ignore
                    except Exception:
                        data = {"choices": [{"message": {"role": "assistant", "content": str(resp)}, "finish_reason": "stop"}], "model": resolved, "id": f"openrouter-{uuid.uuid4().hex[:8]}"}
                if "choices" not in data:
                    if not _is_mock_allowed():
                        raise RuntimeError("LLM provider unavailable: openrouter — mock fallback disabled in production")
                    return _mock(resolved, messages, tools=tools)
                data.setdefault("object", "chat.completion")
                data.setdefault("created", int(time.time()))
                data.setdefault("model", resolved)
                return data  # type: ignore
            except Exception:
                if not _is_mock_allowed():
                    raise RuntimeError("LLM provider unavailable: openrouter — mock fallback disabled in production")
                # fall through to httpx path as fallback before mock

        # Raw httpx path (also fallback when openai SDK fails)
        try:
            import httpx  # type: ignore

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/openit-ai/open-agent-os",
                "X-Title": "Open Agent OS",
            }
            payload: dict[str, Any] = {"model": resolved, "messages": messages}
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = kwargs.get("tool_choice", "auto")
            for k in ("temperature", "max_tokens", "top_p"):
                if k in kwargs:
                    payload[k] = kwargs[k]
            async with httpx.AsyncClient(timeout=kwargs.get("timeout_s", 30.0)) as client:
                resp = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                if resp.status_code < 400:
                    data = resp.json()
                    data.setdefault("object", "chat.completion")
                    data.setdefault("model", resolved)
                    data.setdefault("created", int(time.time()))
                    # Normalize choices.tool_calls if present
                    return data
                if not _is_mock_allowed():
                    raise RuntimeError("LLM provider unavailable: openrouter — mock fallback disabled in production")
                return _mock(resolved, messages, tools=tools)
        except Exception:
            if not _is_mock_allowed():
                raise RuntimeError("LLM provider unavailable: openrouter — mock fallback disabled in production")
            return _mock(resolved, messages, tools=tools)

    async def health_check(self) -> dict[str, Any]:
        if not self.api_key:
            return {"status": "no_key", "ok": False}
        try:
            import httpx  # type: ignore

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"})
                return {"status": "ok" if resp.status_code < 400 else "failed", "ok": resp.status_code < 400, "status_code": resp.status_code}
        except Exception as e:
            return {"status": "error", "ok": False, "error": str(e)[:200]}
