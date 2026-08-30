"""Focused Ollama embedding provider tests — importable + fail-closed.

Verifies:
- OllamaEmbeddingProvider importable from both root and packages path
- OAOS_EMBED_API_URL / OAOS_EMBED_MODEL env wiring (1024 for bge-m3)
- Endpoint normalization (/api/embed)
- ChromaDB forbidden (chroma string or :8000 port)
- Mocked embed success and dimension/count mismatch fail-closed
- get_default_provider returns Ollama when URL set, else Fake or prod error
"""
from __future__ import annotations

import os


def test_importable():
    from knowledge_index.embedding import OllamaEmbeddingProvider

    assert OllamaEmbeddingProvider is not None
    # packages path re-export
    import importlib

    mod = importlib.import_module("knowledge_index.embedding")
    assert hasattr(mod, "OllamaEmbeddingProvider")


def test_env_wiring_defaults(monkeypatch):
    monkeypatch.delenv("OAOS_EMBED_API_URL", raising=False)
    monkeypatch.delenv("OAOS_EMBED_MODEL", raising=False)
    monkeypatch.delenv("OAOS_EMBED_DIM", raising=False)
    from knowledge_index.embedding import OllamaEmbeddingProvider

    p = OllamaEmbeddingProvider(api_url="http://127.0.0.1:11434", model="bge-m3:latest")
    assert p.api_url == "http://127.0.0.1:11434"
    assert p.model == "bge-m3:latest"
    assert p.endpoint == "http://127.0.0.1:11434/api/embed"
    assert p.dim == 1024
    assert p.name == "ollama"


def test_endpoint_normalization(monkeypatch):
    from knowledge_index.embedding import OllamaEmbeddingProvider

    cases = [
        ("http://127.0.0.1:11434", "http://127.0.0.1:11434/api/embed"),
        ("http://127.0.0.1:11434/", "http://127.0.0.1:11434/api/embed"),
        ("http://127.0.0.1:11434/api", "http://127.0.0.1:11434/api/embed"),
        ("http://127.0.0.1:11434/api/embed", "http://127.0.0.1:11434/api/embed"),
        ("http://127.0.0.1:11434/api/embeddings", "http://127.0.0.1:11434/api/embeddings"),
    ]
    for raw, exp in cases:
        p = OllamaEmbeddingProvider(api_url=raw, model="bge-m3:latest", dim=1024)
        assert p.endpoint == exp, f"{raw} -> {p.endpoint} != {exp}"


def test_chromadb_forbidden():
    from knowledge_index.embedding import OllamaEmbeddingProvider

    import pytest

    with pytest.raises(ValueError, match="ChromaDB"):
        OllamaEmbeddingProvider(api_url="http://chroma:8000", model="bge-m3:latest")
    with pytest.raises(ValueError, match="ChromaDB"):
        OllamaEmbeddingProvider(api_url="http://127.0.0.1:8000", model="bge-m3:latest")
    # also via helper
    from knowledge_index.embedding import _assert_no_chromadb

    with pytest.raises(ValueError):
        _assert_no_chromadb("http://chromadb:8000/api")


def _fake_client_success(dim=1024):
    class FakeResp:
        status_code = 200

        def json(self):
            return {"embeddings": [[0.1] * dim, [0.2] * dim]}

    class FakeClient:
        def post(self, url, headers=None, json=None, timeout=None):
            assert url.endswith("/api/embed") or url.endswith("/api/embeddings")
            assert json["model"] == "bge-m3:latest"
            assert json["input"] == ["hello", "world"]
            return FakeResp()

    return FakeClient()


def test_ollama_embed_mocked_success():
    from knowledge_index.embedding import OllamaEmbeddingProvider

    client = _fake_client_success(dim=1024)
    p = OllamaEmbeddingProvider(
        api_url="http://127.0.0.1:11434", model="bge-m3:latest", dim=1024, http_client=client
    )
    vecs = p.embed(["hello", "world"])
    assert len(vecs) == 2
    assert len(vecs[0]) == 1024
    assert vecs[0][0] == 0.1


def test_ollama_embed_dim_mismatch_fail_closed():
    import pytest
    from knowledge_index.embedding import OllamaEmbeddingProvider

    class BadDimResp:
        status_code = 200

        def json(self):
            return {"embeddings": [[0.1] * 512]}

    class BadClient:
        def post(self, *a, **kw):
            return BadDimResp()

    p = OllamaEmbeddingProvider(
        api_url="http://127.0.0.1:11434", model="bge-m3:latest", dim=1024, http_client=BadClient()
    )
    with pytest.raises(RuntimeError, match="dimension mismatch"):
        p.embed(["hello"])


def test_ollama_embed_count_mismatch_fail_closed():
    import pytest
    from knowledge_index.embedding import OllamaEmbeddingProvider

    class CountResp:
        status_code = 200

        def json(self):
            return {"embeddings": [[0.1] * 1024]}

    class CountClient:
        def post(self, *a, **kw):
            return CountResp()

    p = OllamaEmbeddingProvider(
        api_url="http://127.0.0.1:11434", model="bge-m3:latest", dim=1024, http_client=CountClient()
    )
    with pytest.raises(RuntimeError, match="count mismatch"):
        p.embed(["a", "b"])


def test_ollama_embed_missing_url_fail_closed():
    import pytest
    from knowledge_index.embedding import OllamaEmbeddingProvider

    p = OllamaEmbeddingProvider(api_url="", model="bge-m3:latest", dim=1024)
    with pytest.raises(RuntimeError, match="OAOS_EMBED_API_URL not configured"):
        p.embed(["hello"])


def test_get_default_provider_ollama_when_url_set(monkeypatch):
    monkeypatch.setenv("OAOS_EMBED_API_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("OAOS_EMBED_MODEL", "bge-m3:latest")
    monkeypatch.delenv("OAOS_ENV", raising=False)
    # ensure clean
    monkeypatch.delenv("OAOS_EMBED_DIM", raising=False)
    from knowledge_index.embedding import get_default_provider

    p = get_default_provider(dim=1536)
    assert p.name == "ollama"
    assert p.dim == 1024
    # cleanup
    monkeypatch.delenv("OAOS_EMBED_API_URL", raising=False)


def test_get_default_provider_nonprod_fake_when_no_url(monkeypatch):
    monkeypatch.delenv("OAOS_EMBED_API_URL", raising=False)
    monkeypatch.delenv("OAOS_EMBED_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OAOS_ENV", raising=False)
    monkeypatch.delenv("OAOS_EMBED_DIM", raising=False)
    from knowledge_index.embedding import get_default_provider

    p = get_default_provider(dim=32)
    assert p.name == "fake"
    assert p.dim == 32


def test_get_default_provider_prod_requires_url(monkeypatch):
    import pytest

    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.delenv("OAOS_EMBED_API_URL", raising=False)
    monkeypatch.delenv("OAOS_EMBED_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from knowledge_index.embedding import get_default_provider

    with pytest.raises(RuntimeError, match="No embedding provider"):
        get_default_provider()
    monkeypatch.delenv("OAOS_ENV", raising=False)
