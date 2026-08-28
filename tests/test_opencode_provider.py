"""tests/test_opencode_provider.py — OpenCode binary + HTTP + mock chain, health, streaming."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make agent_runtime importable without install
ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packages" / "agent-runtime"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

from agent_runtime.providers.opencode import (
    OpenCodeProvider,
    resolve_binary_path,
    resolve_project_path,
)


# -- helper: make executable temp binary --
def _make_fake_bin(tmp_path: Path, name: str = "opencode", stdout: str = '{"choices":[{"index":0,"message":{"role":"assistant","content":"bin hi"}}]}') -> Path:
    bin_path = tmp_path / name
    bin_path.write_text(f"#!/bin/sh\necho '{stdout}'\n")
    bin_path.chmod(0o755)
    return bin_path


# 1. Binary detection via OPENCODE_BIN
def test_resolve_binary_env(tmp_path):
    fake = _make_fake_bin(tmp_path)
    os.environ["OPENCODE_BIN"] = str(fake)
    try:
        assert resolve_binary_path() == str(fake)
    finally:
        del os.environ["OPENCODE_BIN"]


def test_resolve_binary_which(monkeypatch):
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    monkeypatch.delenv("OPENCODE_BINARY", raising=False)
    with patch("agent_runtime.providers.opencode.shutil.which", return_value="/usr/local/bin/opencode"):
        with patch("agent_runtime.providers.opencode._is_executable", return_value=False):
            # if which returns path, resolve_binary should return it
            # need to ensure _is_executable for which path not checked? which path is returned directly
            # So patch _is_executable only for common paths; which result is returned before check
            result = resolve_binary_path()
            assert result == "/usr/local/bin/opencode"


def test_path_field_binary_vs_project(tmp_path):
    fake_bin = _make_fake_bin(tmp_path)
    # path that is executable binary
    p = OpenCodeProvider(path=str(fake_bin))
    assert p._get_binary() == str(fake_bin.resolve())
    assert p._get_project_dir() is None

    # path as project dir
    proj = tmp_path / "myproj"
    proj.mkdir()
    p2 = OpenCodeProvider(path=str(proj))
    assert resolve_project_path(str(proj)) == str(proj.resolve())
    # binary should not be resolved from project dir alone (unless which finds)
    # but _get_binary with project path should attempt which, not return project
    # we test resolve_project_path directly
    assert p2._get_project_dir() == str(proj.resolve())


def test_explicit_binary_overrides_path(tmp_path):
    fake_bin = _make_fake_bin(tmp_path, name="myopencode")
    proj = tmp_path / "proj2"
    proj.mkdir()
    p = OpenCodeProvider(path=str(proj), binary_path=str(fake_bin))
    assert p._get_binary() == str(fake_bin.resolve()) or str(fake_bin) in p._get_binary()
    assert p._get_project_dir() == str(proj.resolve())


# 2. HTTP success path (httpx -> binary -> mock, http wins)
@pytest.mark.asyncio
async def test_http_success_returns_http():
    prov = OpenCodeProvider(base_url="http://localhost:4096", model="qwen3-coder")
    with patch("httpx.AsyncClient") as MockClient:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "op-1",
            "object": "chat.completion",
            "model": "qwen3-coder",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "http hi"}, "finish_reason": "stop"}],
        }
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await prov.call([{"role": "user", "content": "hello"}])
        assert result["choices"][0]["message"]["content"] == "http hi"
        assert result["id"] == "op-1"


@pytest.mark.asyncio
async def test_http_fallback_to_binary(tmp_path):
    fake_bin = _make_fake_bin(tmp_path, stdout='{"content":"binary fallback hi"}')
    prov = OpenCodeProvider(base_url="http://localhost:9999", model="qwen3-coder", path=str(fake_bin))
    # httpx fails -> binary succeeds
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("conn refused"))
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await prov.call([{"role": "user", "content": "hi"}], timeout_s=2)
        assert "binary fallback hi" in result["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_binary_fallback_to_mock(monkeypatch):
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    monkeypatch.delenv("OPENCODE_BINARY", raising=False)
    prov = OpenCodeProvider(base_url="http://localhost:1", model="deepseek-v3")
    # force no binary
    with patch("agent_runtime.providers.opencode.resolve_binary_path", return_value=None), patch("agent_runtime.providers.opencode_go.resolve_binary_path", return_value=None):
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("no server"))
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await prov.call([{"role": "user", "content": "ping mock"}])
            assert "mock:opencode:deepseek-v3" in result["choices"][0]["message"]["content"]
            assert "ping mock" in result["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_call_with_stream_true(tmp_path):
    fake_bin = _make_fake_bin(tmp_path, stdout='{"content":"stream full"}')
    prov = OpenCodeProvider(base_url="http://localhost:1", model="qwen3-coder", path=str(fake_bin))
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("no http"))
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
        # stream=True should collect streaming chunks into completion
        result = await prov.call([{"role": "user", "content": "hi"}], stream=True, timeout_s=2)
        assert result["object"] == "chat.completion"
        assert "choices" in result


# 3. Health check — http ok, binary ok
@pytest.mark.asyncio
async def test_health_check_http_and_binary(tmp_path):
    fake_bin = _make_fake_bin(tmp_path)
    prov = OpenCodeProvider(base_url="http://localhost:4096", path=str(fake_bin))
    with patch("httpx.AsyncClient") as MockClient:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="opencode 1.2.3\n", stderr="")
            health = await prov.health_check()
            assert health["status"] == "ok"
            assert health["http_ok"] is True
            assert health["binary_ok"] is True
            assert "1.2.3" in health["binary_version"]


@pytest.mark.asyncio
async def test_health_check_unavailable(monkeypatch):
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    prov = OpenCodeProvider(base_url="http://localhost:1")
    with patch("agent_runtime.providers.opencode.resolve_binary_path", return_value=None), patch("agent_runtime.providers.opencode_go.resolve_binary_path", return_value=None):
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("refused"))
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            health = await prov.health_check()
            assert health["status"] == "unavailable"
            assert health["http_ok"] is False
            assert health["binary_ok"] is False


# 4. Streaming
@pytest.mark.asyncio
async def test_stream_mock_fallback():
    prov = OpenCodeProvider(base_url="http://localhost:1", model="qwen3-coder")
    with patch("agent_runtime.providers.opencode.resolve_binary_path", return_value=None), patch("agent_runtime.providers.opencode_go.resolve_binary_path", return_value=None):
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("no http"))
            # need stream and get mocks
            mock_client.get = AsyncMock(side_effect=Exception("no http"))
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            # also need client.stream mock failure path
            mock_client.stream = MagicMock(side_effect=Exception("no stream"))
            chunks = []
            async for ch in prov.stream([{"role": "user", "content": "stream test"}]):
                chunks.append(ch)
            assert len(chunks) >= 2
            assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
            # first chunk should have content delta
            assert "content" in chunks[0]["choices"][0]["delta"]


@pytest.mark.asyncio
async def test_stream_via_http_sse():
    prov = OpenCodeProvider(base_url="http://localhost:4096", model="qwen3-coder")
    with patch("httpx.AsyncClient") as MockClient:
        # simulate SSE streaming response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        async def fake_aiter():
            for line in ['data: {"id":"c1","object":"chat.completion.chunk","model":"qwen3-coder","choices":[{"index":0,"delta":{"content":"hello "},"finish_reason":null}]}', 'data: {"id":"c1","object":"chat.completion.chunk","model":"qwen3-coder","choices":[{"index":0,"delta":{"content":"world"},"finish_reason":null}]}', 'data: [DONE]']:
                yield line
        mock_resp.aiter_lines = lambda: fake_aiter()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=mock_ctx)
        mock_client.post = AsyncMock()
        mock_client.get = AsyncMock()
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
        chunks = []
        async for ch in prov.stream([{"role": "user", "content": "hi"}]):
            chunks.append(ch)
            if len(chunks) >= 2:
                break
        assert len(chunks) >= 1
        # at least one chunk has content
        assert any("hello" in str(c) or "world" in str(c) for c in chunks)


# Provider registry exposure
def test_provider_map_includes_opencode():
    from agent_runtime.providers import PROVIDER_MAP
    from agent_runtime.providers.opencode_go import OpenCodeProvider as GoProvider
    assert "opencode" in PROVIDER_MAP
    assert "opencode-go" in PROVIDER_MAP
    assert PROVIDER_MAP["opencode"] is GoProvider
    assert PROVIDER_MAP["opencode-go"] is GoProvider


def test_call_via_binary_plain_text(tmp_path):
    # ensure binary plain text (non-json) is wrapped correctly — via thread
    fake_bin = tmp_path / "opencode"
    fake_bin.write_text("#!/bin/sh\necho 'plain output text'\n")
    fake_bin.chmod(0o755)
    prov = OpenCodeProvider(base_url="http://localhost:1", model="qwen3-coder", path=str(fake_bin))
    # call via binary directly
    import asyncio
    result = asyncio.run(prov._call_via_binary([{"role": "user", "content": "hi"}], "qwen3-coder", timeout_s=2))
    assert result is not None
    assert "plain output text" in result["choices"][0]["message"]["content"]
