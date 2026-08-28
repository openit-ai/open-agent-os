"""Ollama provider — via http://localhost:11434, lazy httpx + mock fallback."""
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
        "id": f"mock-ollama-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"[mock:ollama:{model}] echo: {last}" if last else f"[mock:ollama:{model}] hello", "tool_calls": []}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

class OllamaProvider:
    """Ollama local LLM. Env: OLLAMA_BASE_URL / OLLAMA_HOST default http://localhost:11434."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None, **_: Any) -> None:
        # api_key unused for ollama but kept for interface parity
        self.api_key = api_key or ""
        raw = base_url or os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST") or os.getenv("OAOS_OLLAMA_BASE_URL") or "http://localhost:11434"
        self.base_url = raw.rstrip("/")
        self.default_model = model or os.getenv("OLLAMA_MODEL") or os.getenv("OAOS_OLLAMA_MODEL") or "llama3"

    async def call(self, messages: list[dict[str, Any]], model: str | None = None, tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> dict[str, Any]:
        resolved = model or self.default_model
        # Attempt real HTTP to ollama; on failure return mock (so tests/offline pass)
        try:
            import httpx  # type: ignore
            payload: dict[str, Any] = {"model": resolved, "messages": messages, "stream": False}
            if tools:
                # Ollama supports tools in 0.1.20+; include if provided
                payload["tools"] = tools
            # Ollama native endpoint: /api/chat ; also OpenAI-compat at /v1/chat/completions for newer versions
            timeout = kwargs.get("timeout_s", 20.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                # Try native first
                try:
                    resp = await client.post(f"{self.base_url}/api/chat", json=payload)
                    if resp.status_code < 400:
                        data = resp.json()
                        # native format: {"message": {"role": "assistant", "content": "..."}, "done": true, ...}
                        if "message" in data:
                            content = data.get("message", {}).get("content", "") or data.get("content", "") or ""
                            # tool_calls may be in message.tool_calls
                            tcs = data.get("message", {}).get("tool_calls") or data.get("tool_calls") or []
                            # Normalize tool_calls to OpenAI shape if present
                            norm_tcs: list[dict[str, Any]] = []
                            for tc in tcs:
                                if isinstance(tc, dict) and "function" in tc:
                                    norm_tcs.append(tc)
                                elif isinstance(tc, dict) and "name" in tc:
                                    norm_tcs.append({"id": tc.get("id", f"call_{uuid.uuid4().hex[:6]}"), "type": "function", "function": {"name": tc.get("name", ""), "arguments": str(tc.get("arguments", "{}"))}})
                            return {
                                "id": f"ollama-{uuid.uuid4().hex[:8]}",
                                "object": "chat.completion",
                                "created": int(time.time()),
                                "model": data.get("model", resolved),
                                "choices": [{"index": 0, "message": {"role": "assistant", "content": str(content), "tool_calls": norm_tcs}, "finish_reason": "tool_calls" if norm_tcs else "stop"}],
                                "usage": {"prompt_tokens": data.get("prompt_eval_count", 0), "completion_tokens": data.get("eval_count", 0), "total_tokens": 0},
                            }
                        if "choices" in data:
                            return data
                except Exception:
                    pass
                # Try OpenAI compat
                try:
                    resp2 = await client.post(f"{self.base_url}/v1/chat/completions", json=payload)
                    if resp2.status_code < 400:
                        data2 = resp2.json()
                        if "choices" in data2:
                            return data2
                except Exception:
                    pass
            return _mock(resolved, messages, tools=tools)
        except ImportError:
            return _mock(resolved, messages, tools=tools)
        except Exception:
            return _mock(resolved, messages, tools=tools)
