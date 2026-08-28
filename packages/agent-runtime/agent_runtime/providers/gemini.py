"""Gemini provider — via google-genai (google.genai) or google.generativeai, lazy + mock fallback."""
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
        "id": f"mock-gemini-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"[mock:gemini:{model}] echo: {last}" if last else f"[mock:gemini:{model}] hello", "tool_calls": []}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

class GeminiProvider:
    """Google Gemini. Env: GOOGLE_API_KEY / GEMINI_API_KEY / GOOGLE_GENAI_API_KEY."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None, **_: Any) -> None:
        self.api_key = api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_GENAI_API_KEY") or os.getenv("OAOS_GEMINI_API_KEY") or ""
        self.base_url = base_url  # google genai uses api_key only
        self.default_model = model or os.getenv("GEMINI_MODEL") or os.getenv("GOOGLE_GEMINI_MODEL") or "gemini-1.5-flash"

    async def call(self, messages: list[dict[str, Any]], model: str | None = None, tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> dict[str, Any]:
        resolved = model or self.default_model
        if not self.api_key:
            return _mock(resolved, messages, tools=tools)
        # Try google.genai (new SDK)
        try:
            from google import genai  # type: ignore
            client = genai.Client(api_key=self.api_key)  # type: ignore
            # Translate messages: join into single prompt + system
            # google genai expects contents with role user/model
            # For simplicity, concatenate user/assistant messages
            prompt_parts: list[str] = []
            system_prompt = ""
            for m in messages:
                role = m.get("role", "")
                content = str(m.get("content", ""))
                if role == "system":
                    system_prompt += content + "\n"
                elif role == "user":
                    prompt_parts.append(f"User: {content}")
                elif role == "assistant":
                    prompt_parts.append(f"Assistant: {content}")
                elif role == "tool":
                    prompt_parts.append(f"Tool:{m.get('name','')} {content}")
            full_prompt = (system_prompt + "\n".join(prompt_parts)).strip() or "hello"
            # Use async if available else thread
            try:
                # genai sync API — run in thread
                import asyncio
                def _sync_call() -> str:
                    resp = client.models.generate_content(model=resolved, contents=full_prompt)  # type: ignore
                    # Extract text
                    try:
                        txt = getattr(resp, "text", "") or ""
                        if not txt:
                            # try candidates
                            cands = getattr(resp, "candidates", None) or []
                            if cands:
                                parts = getattr(cands[0], "content", None)
                                if parts:
                                    p_list = getattr(parts, "parts", None) or []
                                    txt = " ".join(getattr(p, "text", "") for p in p_list)
                        return txt or str(resp)
                    except Exception:
                        return str(resp)
                text = await asyncio.to_thread(_sync_call)
                return {
                    "id": f"gemini-{uuid.uuid4().hex[:8]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": resolved,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text, "tool_calls": []}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
            except Exception:
                return _mock(resolved, messages, tools=tools)
        except ImportError:
            pass
        # Fallback try old SDK google.generativeai
        try:
            import google.generativeai as genai_old  # type: ignore
            genai_old.configure(api_key=self.api_key)  # type: ignore
            import asyncio
            def _old_call() -> str:
                mdl = genai_old.GenerativeModel(resolved)  # type: ignore
                # Build prompt: last user content
                last_user = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user = str(m.get("content", ""))
                        break
                resp = mdl.generate_content(last_user or "hello")  # type: ignore
                try:
                    return getattr(resp, "text", "") or str(resp)
                except Exception:
                    return str(resp)
            text = await asyncio.to_thread(_old_call)
            return {
                "id": f"gemini-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": resolved,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text, "tool_calls": []}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        except ImportError:
            return _mock(resolved, messages, tools=tools)
        except Exception:
            return _mock(resolved, messages, tools=tools)
