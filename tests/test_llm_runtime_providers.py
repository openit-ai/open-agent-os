"""Test multi-provider adapter — ollama mock + hermes runtime_mode + provider dispatch."""
from __future__ import annotations

import os
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from agent_runtime.llm_runtime import (
    LLMProviderAdapter,
    StructuredToolLoop,
    ProviderType,
    RuntimeMode,
    ToolOutputLimits,
    OAOSContext,
)
from agent_runtime.providers.ollama import OllamaProvider
from agent_runtime.providers.claude import ClaudeProvider
from agent_runtime.providers.codex import CodexProvider
from agent_runtime.providers.gemini import GeminiProvider
from agent_runtime.providers.opencode import OpenCodeProvider


# ── ProviderType enum ────────────────────────────────────────────────
def test_provider_type_enum():
    assert ProviderType.CLAUDE.value == "claude"
    assert ProviderType.CODEX.value == "codex"
    assert ProviderType.GEMINI.value == "gemini"
    assert ProviderType.OPENCODE_GO.value == "opencode-go"
    assert ProviderType.OPENROUTER.value == "openrouter"
    assert ProviderType.OLLAMA.value == "ollama"
    assert ProviderType.OPENCODE.value == "opencode"  # alias
    assert ProviderType.from_str("OLLAMA") == ProviderType.OLLAMA
    assert ProviderType.from_str("opencode") == ProviderType.OPENCODE_GO  # alias normalization
    assert ProviderType.from_str("unknown") is None
    assert len(list(ProviderType)) == 7


def test_runtime_mode_enum():
    assert RuntimeMode.LLM.value == "llm"
    assert RuntimeMode.HERMES.value == "hermes"
    assert RuntimeMode.from_str("hermes") == RuntimeMode.HERMES
    assert RuntimeMode.from_str("llm") == RuntimeMode.LLM
    assert RuntimeMode.from_str(None) == RuntimeMode.LLM
    assert RuntimeMode.from_str("") == RuntimeMode.LLM


# ── Providers have call() ───────────────────────────────────────────
def test_providers_have_call():
    from agent_runtime.providers.openrouter import OpenRouterProvider
    from agent_runtime.providers.opencode_go import OpenCodeProvider as OpenCodeGoProvider
    for cls in [ClaudeProvider, CodexProvider, GeminiProvider, OpenCodeProvider, OpenCodeGoProvider, OpenRouterProvider, OllamaProvider]:
        inst = cls()
        assert hasattr(inst, "call")
        assert callable(getattr(inst, "call"))


# ── Ollama mock test (required) ─────────────────────────────────────
@pytest.mark.asyncio
async def test_ollama_mock_via_adapter():
    """Ollama provider via LLMProviderAdapter — mock httpx returns OpenAI-compatible dict."""
    adapter = LLMProviderAdapter(provider="ollama", model="llama3", timeout_s=5.0, max_retries=0)
    assert adapter.provider_type == ProviderType.OLLAMA
    assert adapter.runtime_mode == RuntimeMode.LLM

    # Mock httpx inside OllamaProvider.call
    with patch("httpx.AsyncClient") as MockClient:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "message": {"role": "assistant", "content": "ollama mock says hi"},
            "model": "llama3",
            "done": True,
        }
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

        messages = [{"role": "user", "content": "hello ollama"}]
        result = await adapter.completion(messages, trace_id="trace-ollama-1")
        assert result["model"] == "llama3"
        assert result["choices"][0]["message"]["content"] == "ollama mock says hi"
        assert result["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_ollama_provider_direct_call_mock():
    """Direct OllamaProvider.call() with httpx mock."""
    prov = OllamaProvider(base_url="http://localhost:11434", model="llama3")
    with patch("httpx.AsyncClient") as MockClient:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "message": {"role": "assistant", "content": "direct ollama content"},
            "model": "llama3",
            "done": True,
        }
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await prov.call([{"role": "user", "content": "ping"}])
        assert "direct ollama content" in result["choices"][0]["message"]["content"]


# ── Config from env ─────────────────────────────────────────────────
def test_config_from_env():
    os.environ["OLLAMA_BASE_URL"] = "http://ollama-test:11434"
    os.environ["OLLAMA_MODEL"] = "llama3-test"
    # Need to clear cached provider instance so new env is read
    adapter = LLMProviderAdapter(provider="ollama")
    assert adapter.provider_config.get("ollama_base_url") == "http://ollama-test:11434"
    assert adapter.provider_config.get("ollama_model") == "llama3-test"
    del os.environ["OLLAMA_BASE_URL"]
    del os.environ["OLLAMA_MODEL"]

    # Also test OPENAI env for codex
    os.environ["OPENAI_API_KEY"] = "sk-test-codex"
    adapter2 = LLMProviderAdapter(provider="codex")
    assert adapter2.provider_config.get("codex_api_key") == "sk-test-codex"
    del os.environ["OPENAI_API_KEY"]


def test_config_from_admin_api_mock(monkeypatch):
    """Fetching config from admin-console API (mocked httpx.Client)."""
    monkeypatch.setenv("ADMIN_CONSOLE_URL", "http://admin-api:8010")
    monkeypatch.setenv("ADMIN_API_TOKEN", "tok")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"provider": "ollama", "model": "llama3-admin", "base_url": "http://admin-ollama:11434"}

    with patch("httpx.Client") as MockClient:
        mock_c = MagicMock()
        mock_c.get.return_value = mock_resp
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_c)
        MockClient.return_value.__exit__ = MagicMock(return_value=None)
        adapter = LLMProviderAdapter(provider="ollama")
        # admin_api should override env
        assert adapter.provider_config.get("model") == "llama3-admin" or adapter.provider_config.get("ollama_model") == "llama3-admin" or adapter.provider_config.get("base_url") == "http://admin-ollama:11434"

    monkeypatch.delenv("ADMIN_CONSOLE_URL", raising=False)
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)


# ── Hermes runtime_mode bypass ─────────────────────────────────────
@pytest.mark.asyncio
async def test_hermes_mode_bypasses_provider():
    """If runtime_mode == hermes, provider config is empty and call delegates to hermes API."""
    adapter = LLMProviderAdapter(provider="ollama", model="llama3", runtime_mode="hermes", hermes_api_url="http://hermes:8001")
    assert adapter.runtime_mode == RuntimeMode.HERMES
    assert adapter.provider_config == {}  # no provider config in hermes mode

    with patch("httpx.AsyncClient") as MockClient:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "hermes-123",
            "object": "chat.completion",
            "model": "hermes-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hermes delegated"}, "finish_reason": "stop"}],
        }
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await adapter.completion([{"role": "user", "content": "hi"}])
        assert "hermes delegated" in result["choices"][0]["message"]["content"]
        # Verify hermes URL was called
        called_url = mock_client.post.call_args[0][0]
        assert "hermes" in called_url


@pytest.mark.asyncio
async def test_hermes_mode_via_env():
    """Env OAOS_RUNTIME_MODE=hermes forces hermes delegation even if provider env set."""
    os.environ["OAOS_RUNTIME_MODE"] = "hermes"
    os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434"
    adapter = LLMProviderAdapter(provider="ollama")
    assert adapter.runtime_mode == RuntimeMode.HERMES
    assert adapter.provider_config == {}
    del os.environ["OAOS_RUNTIME_MODE"]
    del os.environ["OLLAMA_BASE_URL"]


# ── StructuredToolLoop still works with new adapter ─────────────────
@pytest.mark.asyncio
async def test_structured_tool_loop_still_works_with_ollama_provider():
    """StructuredToolLoop must work with ollama provider adapter (and hermes mode)."""
    # Mock gateway
    async def fake_gateway(tool_name, arguments, trace_id, **kw):
        return {"tool": tool_name, "result": f"gateway:{tool_name}:{arguments}", "content": f"result for {tool_name}"}

    # Adapter mocked to return tool_calls then done
    adapter = LLMProviderAdapter(provider="ollama", model="llama3", timeout_s=5.0, max_retries=0)

    # Queue responses: first has tool_call, second is final
    tool_resp = {
        "id": "resp1",
        "object": "chat.completion",
        "model": "llama3",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search", "arguments": '{"q": "hi"}'}}],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    done_resp = {
        "id": "resp2",
        "object": "chat.completion",
        "model": "llama3",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "final answer", "tool_calls": []}, "finish_reason": "stop"}],
    }
    adapter.push_mock_response(tool_resp)
    adapter.push_mock_response(done_resp)

    loop = StructuredToolLoop(llm=adapter, gateway=fake_gateway, max_steps=5)
    messages = [{"role": "user", "content": "test"}]
    result = await loop.run(messages, trace_id="trace-loop-1", tools=[{"type": "function", "function": {"name": "search", "description": "search", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}])
    assert result["steps"] >= 1
    assert result["terminated"] in ("done", "max_steps")
    assert len(result["messages"]) >= 3  # user + assistant+tool + final

# ── P1: mock fallback disabled in production ─────────────────────
def test_mock_blocked_in_production():
    """OAOS_ENV=production must block mock fallback — provider call raises without key."""
    import os
    from agent_runtime.providers.openrouter import OpenRouterProvider
    os.environ["OAOS_ENV"] = "production"
    os.environ.pop("OAOS_MOCK_FALLBACK", None)
    try:
        prov = OpenRouterProvider(api_key="", base_url="https://openrouter.ai/api/v1", model="openrouter/auto")
        import asyncio
        try:
            asyncio.run(prov.call([{"role": "user", "content": "hi"}]))
            assert False, "should have raised RuntimeError in production without key"
        except RuntimeError as e:
            assert "mock fallback disabled" in str(e)
    finally:
        os.environ.pop("OAOS_ENV", None)

def test_mock_allowed_with_explicit_flag_in_production():
    """H7 immutable: OAOS_MOCK_FALLBACK=1 must NOT override production block — fail-closed."""
    import os, asyncio, pytest
    from agent_runtime.providers.openrouter import OpenRouterProvider
    os.environ["OAOS_ENV"] = "production"
    os.environ["OAOS_MOCK_FALLBACK"] = "1"
    try:
        prov = OpenRouterProvider(api_key="", base_url="https://openrouter.ai/api/v1", model="openrouter/auto")
        # H7: prod mock is immutable, must fail-closed even with flag
        with pytest.raises(RuntimeError, match="mock fallback disabled"):
            asyncio.run(prov.call([{"role": "user", "content": "hi prod override"}]))
    finally:
        os.environ.pop("OAOS_ENV", None)
        os.environ.pop("OAOS_MOCK_FALLBACK", None)

@pytest.mark.asyncio
async def test_push_mock_still_works_in_production():
    """Explicit push_mock_response must still work even in production."""
    import os
    os.environ["OAOS_ENV"] = "production"
    try:
        adapter = LLMProviderAdapter(provider="ollama", model="llama3", timeout_s=5.0, max_retries=0)
        adapter.push_mock_response({"id": "m1", "object": "chat.completion", "model": "llama3", "choices": [{"index": 0, "message": {"role": "assistant", "content": "explicit mock ok", "tool_calls": []}, "finish_reason": "stop"}]})
        result = await adapter.completion([{"role": "user", "content": "hi"}])
        assert "explicit mock ok" in result["choices"][0]["message"]["content"]
    finally:
        os.environ.pop("OAOS_ENV", None)

