"""Tests: OllamaEmbeddingProvider wiring — TDD, no live API by default, optional live.

Production has Ollama 127.0.0.1:11434 model bge-m3:latest dim 1024.
Fake retained only explicit test/nonprod.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure knowledge-index packages on path (both ROOT and packages)
ROOT = Path(__file__).resolve().parents[1]
for cand in (str(ROOT / "packages" / "knowledge-index"), str(ROOT)):
    if cand not in sys.path:
        sys.path.insert(0, cand)

from knowledge_index.embedding import (
    OllamaEmbeddingProvider,
    FakeEmbeddingProvider,
    get_default_provider,
    _normalize_embed_endpoint,
    _resolve_embed_api_url,
    _resolve_embed_model,
    _resolve_embed_dim,
)


# helpers: mock http_client
class _MockResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class _MockClient:
    def __init__(self, resp: _MockResp):
        self.resp = resp
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return self.resp


class TestEndpointNormalization:
    def test_bare_host(self):
        assert _normalize_embed_endpoint("http://127.0.0.1:11434") == "http://127.0.0.1:11434/api/embed"

    def test_with_api(self):
        assert _normalize_embed_endpoint("http://127.0.0.1:11434/api") == "http://127.0.0.1:11434/api/embed"

    def test_with_embed(self):
        assert _normalize_embed_endpoint("http://127.0.0.1:11434/api/embed") == "http://127.0.0.1:11434/api/embed"

    def test_trailing_slash(self):
        assert _normalize_embed_endpoint("http://127.0.0.1:11434/") == "http://127.0.0.1:11434/api/embed"


class TestEnvResolution:
    def test_resolve_api_url_explicit(self, monkeypatch):
        monkeypatch.setenv("OAOS_EMBED_API_URL", "http://env:11434")
        assert _resolve_embed_api_url("http://explicit:11434") == "http://explicit:11434"
        assert _resolve_embed_api_url() == "http://env:11434"
        monkeypatch.delenv("OAOS_EMBED_API_URL", raising=False)
        monkeypatch.setenv("OLLAMA_HOST", "http://ollama:11434")
        assert _resolve_embed_api_url() == "http://ollama:11434"

    def test_resolve_model_default(self, monkeypatch):
        for k in ("OAOS_EMBED_MODEL", "OAOS_EMBEDDING_MODEL", "OLLAMA_EMBED_MODEL"):
            monkeypatch.delenv(k, raising=False)
        assert _resolve_embed_model() == "bge-m3:latest"
        monkeypatch.setenv("OAOS_EMBED_MODEL", "nomic-embed-text:latest")
        assert _resolve_embed_model() == "nomic-embed-text:latest"

    def test_resolve_dim_bge(self, monkeypatch):
        monkeypatch.delenv("OAOS_EMBED_DIM", raising=False)
        monkeypatch.delenv("OAOS_EMBEDDING_DIM", raising=False)
        assert _resolve_embed_dim(None, "bge-m3:latest") == 1024
        assert _resolve_embed_dim(1536, "bge-m3:latest") == 1536
        assert _resolve_embed_dim(None, "unknown-model") == 1536
        monkeypatch.setenv("OAOS_EMBED_DIM", "768")
        assert _resolve_embed_dim(None, "bge-m3:latest") == 768


class TestOllamaProviderMocked:
    def test_embed_mocked_success(self):
        dim = 1024
        client = _MockClient(_MockResp(200, {"embeddings": [[0.1] * dim, [0.2] * dim]}))
        p = OllamaEmbeddingProvider(api_url="http://127.0.0.1:11434", model="bge-m3:latest", dim=dim, http_client=client)
        assert p.dim == 1024
        assert p.name == "ollama"
        assert p.model == "bge-m3:latest"
        assert p.endpoint == "http://127.0.0.1:11434/api/embed"
        vecs = p.embed(["hello", "world"])
        assert len(vecs) == 2
        assert len(vecs[0]) == 1024
        # url normalized
        assert client.calls[0]["url"] == "http://127.0.0.1:11434/api/embed"
        assert client.calls[0]["json"]["model"] == "bge-m3:latest"
        assert client.calls[0]["json"]["input"] == ["hello", "world"]

    def test_empty_input(self):
        client = _MockClient(_MockResp(200, {"embeddings": []}))
        p = OllamaEmbeddingProvider(api_url="http://127.0.0.1:11434", dim=1024, http_client=client)
        assert p.embed([]) == []
        assert len(client.calls) == 0  # no HTTP for empty

    def test_fail_closed_missing_url(self):
        p = OllamaEmbeddingProvider(api_url="", model="bge-m3:latest", dim=1024, http_client=_MockClient(_MockResp(200, {"embeddings": [[0.1] * 1024]})))
        # ensure raw_url empty -> embed raises fail-closed
        # construct without env: delenv and explicit empty
        import os as _os
        for k in ("OAOS_EMBED_API_URL", "OAOS_EMBEDDING_API_URL", "OLLAMA_API_URL", "OLLAMA_HOST"):
            _os.environ.pop(k, None)
        p2 = OllamaEmbeddingProvider(api_url="", dim=1024, http_client=_MockClient(_MockResp(200, {})))
        with pytest.raises(RuntimeError, match="OAOS_EMBED_API_URL not configured"):
            p2.embed(["hello"])

    def test_fail_closed_dim_mismatch(self):
        client = _MockClient(_MockResp(200, {"embeddings": [[0.1] * 512]}))  # wrong dim
        p = OllamaEmbeddingProvider(api_url="http://127.0.0.1:11434", dim=1024, http_client=client)
        with pytest.raises(RuntimeError, match="dimension mismatch"):
            p.embed(["hello"])

    def test_fail_closed_count_mismatch(self):
        client = _MockClient(_MockResp(200, {"embeddings": [[0.1] * 1024]}))
        p = OllamaEmbeddingProvider(api_url="http://127.0.0.1:11434", dim=1024, http_client=client)
        with pytest.raises(RuntimeError, match="count mismatch"):
            p.embed(["a", "b"])  # expects 2, got 1

    def test_fail_closed_http_error(self):
        client = _MockClient(_MockResp(500, {"error": "model not found"}))
        p = OllamaEmbeddingProvider(api_url="http://127.0.0.1:11434", dim=1024, http_client=client)
        with pytest.raises(RuntimeError, match="HTTP 500"):
            p.embed(["hello"])

    def test_fail_closed_missing_embeddings_field(self):
        client = _MockClient(_MockResp(200, {"foo": "bar"}))
        p = OllamaEmbeddingProvider(api_url="http://127.0.0.1:11434", dim=1024, http_client=client)
        with pytest.raises(RuntimeError, match="missing 'embeddings'"):
            p.embed(["hello"])

    def test_env_injected_provider(self, monkeypatch):
        monkeypatch.setenv("OAOS_EMBED_API_URL", "http://127.0.0.1:11434")
        monkeypatch.setenv("OAOS_EMBED_MODEL", "bge-m3:latest")
        monkeypatch.setenv("OAOS_EMBED_DIM", "1024")
        client = _MockClient(_MockResp(200, {"embeddings": [[0.1] * 1024]}))
        p = OllamaEmbeddingProvider(http_client=client)  # no explicit args -> env
        assert p.api_url == "http://127.0.0.1:11434"
        assert p.model == "bge-m3:latest"
        vecs = p.embed(["env"])
        assert len(vecs) == 1


class TestGetDefaultProviderWithOllama:
    def test_returns_ollama_when_url_set(self, monkeypatch):
        monkeypatch.setenv("OAOS_EMBED_API_URL", "http://127.0.0.1:11434")
        monkeypatch.setenv("OAOS_EMBED_MODEL", "bge-m3:latest")
        monkeypatch.delenv("OAOS_EMBED_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        # non-prod -> should return Ollama when URL set
        monkeypatch.setenv("OAOS_ENV", "development")
        p = get_default_provider(dim=1536)
        assert p.name == "ollama"
        assert isinstance(p, OllamaEmbeddingProvider)
        # bge-m3 infers 1024 not caller's 1536
        assert p.dim == 1024

    def test_returns_fake_when_no_url_nonprod(self, monkeypatch):
        monkeypatch.delenv("OAOS_EMBED_API_URL", raising=False)
        monkeypatch.delenv("OAOS_EMBEDDING_API_URL", raising=False)
        monkeypatch.delenv("OLLAMA_API_URL", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        monkeypatch.delenv("OAOS_EMBED_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("OAOS_ENV", "development")
        p = get_default_provider(dim=32)
        assert p.name == "fake"
        assert isinstance(p, FakeEmbeddingProvider)

    def test_blocks_in_production_without_url(self, monkeypatch):
        monkeypatch.delenv("OAOS_EMBED_API_URL", raising=False)
        monkeypatch.delenv("OAOS_EMBEDDING_API_URL", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        monkeypatch.delenv("OAOS_EMBED_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("OAOS_ENV", "production")
        monkeypatch.delenv("OAOS_ALLOW_TEST_FIXTURE", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        # Ensure production detection; force prod by OAOS_ENV
        with pytest.raises(RuntimeError, match="No embedding provider configured in production"):
            get_default_provider(dim=1024)

    def test_prod_with_url_returns_ollama_even_in_prod(self, monkeypatch):
        monkeypatch.setenv("OAOS_EMBED_API_URL", "http://127.0.0.1:11434")
        monkeypatch.setenv("OAOS_EMBED_MODEL", "bge-m3:latest")
        monkeypatch.setenv("OAOS_ENV", "production")
        p = get_default_provider(dim=1536)
        assert p.name == "ollama"
        assert p.dim == 1024


class TestOllamaLiveOptional:
    """Live Ollama test — skipped if Ollama not reachable or env not configured.

    Production host has bge-m3 at 127.0.0.1:11434; we verify live embedding dim 1024.
    """

    @pytest.mark.skipif(os.environ.get("OAOS_LIVE_OLLAMA") != "1", reason="set OAOS_LIVE_OLLAMA=1 to run live Ollama test")
    def test_live_ollama_embed(self):
        # relies on env OAOS_EMBED_API_URL=http://127.0.0.1:11434
        p = OllamaEmbeddingProvider(api_url="http://127.0.0.1:11434", model="bge-m3:latest", dim=1024, timeout_s=60)
        vecs = p.embed(["hello world"])
        assert len(vecs) == 1
        assert len(vecs[0]) == 1024
        # normalized? just check not all equal
        assert len(set(vecs[0])) > 10
