"""OpenRouter provider — aggregated gateway via OpenAI-compatible API (https://openrouter.ai)."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

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
        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"[mock:openrouter:{model}] echo: {last}" if last else f"[mock:openrouter:{model}] hello", "tool_calls": []}, "finish_reason": "stop"}],
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
            return _mock(resolved, messages, tools=tools)
        try:
            import httpx  # type: ignore
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "HTTP-Referer": "https://github.com/openit-ai/open-agent-os", "X-Title": "Open Agent OS"}
            payload: dict[str, Any] = {"model": resolved, "messages": messages}
            if tools:
                payload["tools"] = tools
            async with httpx.AsyncClient(timeout=kwargs.get("timeout_s", 30.0)) as client:
                resp = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                if resp.status_code < 400:
                    data = resp.json()
                    data.setdefault("object", "chat.completion")
                    data.setdefault("model", resolved)
                    return data
                return _mock(resolved, messages, tools=tools)
        except Exception:
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
