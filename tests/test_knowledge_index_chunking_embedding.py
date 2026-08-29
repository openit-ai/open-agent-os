"""Tests: chunking + embedding provider boundary (TDD, no live API)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure packages/knowledge-index on path
PKG = Path(__file__).resolve().parents[1] / "packages" / "knowledge-index"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

import pytest
from knowledge_index.chunking import chunk_text, content_hash, make_chunks, ChunkConfig
from knowledge_index.embedding import FakeEmbeddingProvider, HashEmbeddingProvider, get_default_provider


class TestContentHash:
    def test_deterministic(self):
        assert content_hash("hello") == content_hash("hello")
        assert content_hash("hello") != content_hash("hello ")
        assert content_hash("") == content_hash("")
        # known vector
        import hashlib

        assert content_hash("abc") == hashlib.sha256(b"abc").hexdigest()

    def test_empty_and_none(self):
        assert content_hash("") == content_hash(None)  # type: ignore
        assert isinstance(content_hash("x"), str) and len(content_hash("x")) == 64


class TestChunking:
    def test_basic_split(self):
        text = "a" * 2000
        chunks = chunk_text(text, max_chars=800, overlap=100)
        assert len(chunks) >= 2
        # reconstruct: first chunk starts with original, overlap ensures continuity
        assert chunks[0] == text[:800]
        assert chunks[1] == text[700:1500]

    def test_configurable(self):
        text = "x" * 1000
        c_small = chunk_text(text, max_chars=200, overlap=0)
        c_large = chunk_text(text, max_chars=800, overlap=100)
        assert len(c_small) > len(c_large)
        # overlap 0 => step == max_chars
        assert len(c_small) == 5  # 1000/200

    def test_overlap_respected(self):
        text = "abcdefghij" * 100  # 1000 chars
        chunks = chunk_text(text, max_chars=100, overlap=20)
        # step 80 => chunks overlap by 20
        assert chunks[0][80:] == chunks[1][:20]

    def test_empty(self):
        assert chunk_text("") == []
        assert chunk_text("   ") == []
        assert chunk_text("a", max_chars=800) == ["a"]

    def test_clamp(self):
        # max_chars <64 clamped to 64
        chunks = chunk_text("a" * 200, max_chars=10, overlap=5)
        assert len(chunks[0]) == 64
        # overlap >= max_chars clamped to max_chars-1
        chunks2 = chunk_text("a" * 200, max_chars=100, overlap=200)
        assert len(chunks2[0]) == 100
        assert len(chunks2) > 1

    def test_stable_chunk_ids(self):
        rid = "outline/team/doc_001"
        content = "hello world " * 200
        ch = content_hash(content)
        c1 = make_chunks(rid, content, source_content_hash=ch, max_chars=800, overlap=100)
        c2 = make_chunks(rid, content, source_content_hash=ch, max_chars=800, overlap=100)
        assert len(c1) == len(c2)
        for a, b in zip(c1, c2):
            assert a.chunk_id == b.chunk_id
            assert a.content_hash == b.content_hash
            assert a.source_content_hash == ch

    def test_chunk_id_changes_on_content(self):
        rid = "outline/team/doc_001"
        c1 = make_chunks(rid, "hello world", max_chars=800)
        c2 = make_chunks(rid, "hello world changed", max_chars=800)
        assert c1[0].chunk_id != c2[0].chunk_id
        assert c1[0].content_hash != c2[0].content_hash

    def test_different_resource_different_ids(self):
        content = "same content"
        c1 = make_chunks("outline/team/doc_001", content)
        c2 = make_chunks("outline/team/doc_002", content)
        assert c1[0].chunk_id != c2[0].chunk_id


class TestEmbeddingBoundary:
    def test_fake_provider_deterministic(self):
        p = FakeEmbeddingProvider(dim=32)
        v1 = p.embed(["hello"])[0]
        v2 = p.embed(["hello"])[0]
        assert v1 == v2
        assert len(v1) == 32
        # normalized
        import math

        assert abs(math.sqrt(sum(x * x for x in v1)) - 1.0) < 1e-6
        # different text => different vector
        v3 = p.embed(["world"])[0]
        assert v3 != v1

    def test_fake_batch(self):
        p = FakeEmbeddingProvider(dim=16)
        vecs = p.embed(["a", "b", "c"])
        assert len(vecs) == 3
        assert all(len(v) == 16 for v in vecs)

    def test_hash_provider_blocked_in_production(self, monkeypatch):
        monkeypatch.setenv("OAOS_ENV", "production")
        monkeypatch.delenv("OAOS_ALLOW_HASH_EMBED", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delenv("OAOS_ALLOW_TEST_FIXTURE", raising=False)
        # Need to remove PYTEST_CURRENT_TEST that pytest sets — simulate prod
        # hash provider ctor should raise
        with pytest.raises(RuntimeError, match="not allowed in production"):
            HashEmbeddingProvider(dim=16)

    def test_hash_provider_allowed_with_flag_in_production(self, monkeypatch):
        monkeypatch.setenv("OAOS_ENV", "production")
        monkeypatch.setenv("OAOS_ALLOW_HASH_EMBED", "1")
        p = HashEmbeddingProvider(dim=16)
        assert p.embed(["hello"])[0] is not None

    def test_fake_allowed_in_production(self, monkeypatch):
        # Fake is explicit test provider, not blocked by hash guard — but get_default_provider blocks
        monkeypatch.setenv("OAOS_ENV", "production")
        monkeypatch.delenv("OAOS_ALLOW_HASH_EMBED", raising=False)
        p = FakeEmbeddingProvider(dim=8)
        assert p.embed(["x"])[0] is not None

    def test_get_default_provider_blocks_in_production(self, monkeypatch):
        monkeypatch.setenv("OAOS_ENV", "production")
        monkeypatch.delenv("OAOS_EMBED_API_URL", raising=False)
        monkeypatch.delenv("OAOS_EMBED_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OAOS_ALLOW_HASH_EMBED", raising=False)
        # In prod with no provider configured must raise, not silently return hash
        with pytest.raises(RuntimeError, match="No embedding provider"):
            get_default_provider(dim=16)

    def test_get_default_provider_nonprod_returns_fake(self, monkeypatch):
        monkeypatch.setenv("OAOS_ENV", "development")
        monkeypatch.delenv("OAOS_EMBED_API_URL", raising=False)
        monkeypatch.delenv("OAOS_EMBED_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        p = get_default_provider(dim=16)
        assert p.name == "fake"
        assert len(p.embed(["hello"])[0]) == 16

    def test_hash_provider_nonprod_works(self, monkeypatch):
        monkeypatch.setenv("OAOS_ENV", "development")
        p = HashEmbeddingProvider(dim=16)
        assert len(p.embed(["a", "b"])) == 2
