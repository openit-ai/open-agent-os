"""OpenCode provider — via HTTP API (opencode runtime), lazy httpx + mock fallback."""
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
        "id": f"mock-opencode-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"[mock:opencode:{model}] echo: {last}" if last else f"[mock:opencode:{model}] hello", "tool_calls": []}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

class OpenCodeProvider:
    """OpenCode via HTTP API. Env: OPENCODE_API_URL / OPENCODE_BASE_URL default http://localhost:4096."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None, **_: Any) -> None:
        self.api_key = api_key or os.getenv("OPENCODE_API_KEY") or os.getenv("OAOS_OPENCODE_API_KEY") or ""
        self.base_url = (base_url or os.getenv("OPENCODE_API_URL") or os.getenv("OPENCODE_BASE_URL") or os.getenv("OAOS_OPENCODE_BASE_URL") or "http://localhost:4096").rstrip("/")
        self.default_model = model or os.getenv("OPENCODE_MODEL") or "opencode-default"

    async def call(self, messages: list[dict[str, Any]], model: str | None = None, tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> dict[str, Any]:
        resolved = model or self.default_model
        # Try httpx call to opencode endpoint; fallback to mock on any failure
        try:
            import httpx  # type: ignore
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            payload: dict[str, Any] = {"model": resolved, "messages": messages, "stream": False}
            if tools:
                payload["tools"] = tools
            # opencode commonly exposes /v1/chat/completions (OpenAI compat) or /api/chat
            # Try OpenAI-compat first
            url = f"{self.base_url}/v1/chat/completions"
            async with httpx.AsyncClient(timeout=kwargs.get("timeout_s", 15.0)) as client:
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code < 400:
                        data = resp.json()
                        # Normalize
                        if "choices" in data:
                            data.setdefault("object", "chat.completion")
                            data.setdefault("model", resolved)
                            return data
                except Exception:
                    pass
                # fallback try /api/chat
                try:
                    url2 = f"{self.base_url}/api/chat"
                    resp2 = await client.post(url2, json=payload, headers=headers)
                    if resp2.status_code < 400:
                        data2 = resp2.json()
                        if "choices" in data2:
                            return data2
                        # opencode may return {message: {content: ...}}
                        if "message" in data2:
                            content = data2.get("message", {}).get("content", "") or data2.get("content", "")
                            return {
                                "id": data2.get("id", f"opencode-{uuid.uuid4().hex[:8]}"),
                                "object": "chat.completion",
                                "created": int(time.time()),
                                "model": resolved,
                                "choices": [{"index": 0, "message": {"role": "assistant", "content": str(content), "tool_calls": []}, "finish_reason": "stop"}],
                                "usage": data2.get("usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
                            }
                except Exception:
                    pass
            return _mock(resolved, messages, tools=tools)
        except ImportError:
            return _mock(resolved, messages, tools=tools)
        except Exception:
            return _mock(resolved, messages, tools=tools)
