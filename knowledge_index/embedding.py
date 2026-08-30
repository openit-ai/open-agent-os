"""Embedding provider boundary.

Production must NOT silently use hash embeddings. Tests may use FakeProvider.

Design:
- EmbeddingProvider ABC: embed(texts) -> list[vector]; dim property
- FakeEmbeddingProvider: deterministic, no external calls, suitable for tests
- HashEmbeddingProvider: deterministic hash-derived, allowed only when explicitly
  enabled (OAOS_ALLOW_HASH_EMBED=1 or not in production). In production without
  explicit flag it raises.
- get_default_provider() respects env: if OAOS_EMBED_PROVIDER or API key present
  would return real provider (skeleton, not calling live API); otherwise in
  production raises, in non-prod returns Fake for safety (but caller should inject).
"""

from __future__ import annotations

import hashlib
import math
import os
from abc import ABC, abstractmethod
from typing import Any


def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV"):
        v = os.environ.get(k, "").strip().lower()
        if v in ("production", "prod"):
            return True
    return False


def _allow_hash_embed() -> bool:
    flag = os.environ.get("OAOS_ALLOW_HASH_EMBED", "") or os.environ.get("OAOS_ALLOW_FAKE_EMBED", "")
    if flag.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if os.environ.get("OAOS_ALLOW_TEST_FIXTURE", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    if _is_production():
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return False


class EmbeddingProvider(ABC):
    """Abstract embedding provider."""

    @property
    @abstractmethod
    def dim(self) -> int: ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed batch of texts -> list of vectors (len == len(texts), each dim)."""
        ...


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic fake provider for tests.

    Vectors are derived from SHA256(text) -> reproducible, normalized.
    Not suitable for production semantic search but safe for tests.
    """

    def __init__(self, dim: int = 1536, seed: str = "fake") -> None:
        self._dim = int(dim)
        self._seed = seed

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def name(self) -> str:
        return "fake"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        h = hashlib.sha256(f"{self._seed}:{text}".encode("utf-8")).digest()
        # expand via counter mode
        out: list[float] = []
        counter = 0
        seed = h
        while len(out) < self._dim:
            block = hashlib.sha256(seed + counter.to_bytes(4, "little")).digest()
            for b in block:
                out.append((b / 127.5) - 1.0)
                if len(out) >= self._dim:
                    break
            counter += 1
        norm = math.sqrt(sum(x * x for x in out))
        if norm > 0:
            out = [x / norm for x in out]
        return out


class HashEmbeddingProvider(EmbeddingProvider):
    """Hash-derived embedding — deterministic fallback.

    In production this provider raises unless OAOS_ALLOW_HASH_EMBED=1
    or PYTEST_CURRENT_TEST is set. This prevents silent fallback to
    non-semantic vectors in prod.
    """

    def __init__(self, dim: int = 1536) -> None:
        self._dim = int(dim)
        if _is_production() and not _allow_hash_embed():
            raise RuntimeError(
                "HashEmbeddingProvider is not allowed in production. "
                "Configure a real embedding provider (OAOS_EMBED_API_URL + key) "
                "or set OAOS_ALLOW_HASH_EMBED=1 explicitly for testing."
            )

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def name(self) -> str:
        return "hash"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if _is_production() and not _allow_hash_embed():
            raise RuntimeError("hash embeddings blocked in production")
        return [self._hash_one(t) for t in texts]

    def _hash_one(self, text: str) -> list[float]:
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        out: list[float] = []
        counter = 0
        while len(out) < self._dim:
            h = hashlib.sha256(seed + counter.to_bytes(4, "little")).digest()
            for b in h:
                out.append((b / 127.5) - 1.0)
                if len(out) >= self._dim:
                    break
            counter += 1
        norm = math.sqrt(sum(x * x for x in out))
        if norm > 0:
            out = [x / norm for x in out]
        return out


def get_default_provider(dim: int = 1536) -> EmbeddingProvider:
    """Resolve default provider without silently using hash in prod.

    Priority:
    1. If OAOS_EMBED_API_URL or OPENAI_API_KEY configured -> would return
       remote provider (here we raise NotImplemented with guidance to avoid
       claiming live API).
    2. If production -> raise (must configure real provider)
    3. Otherwise -> return FakeEmbeddingProvider for dev/test.

    This function MUST NOT return HashEmbeddingProvider silently in production.
    Tests should inject FakeEmbeddingProvider explicitly.
    """
    api_url = os.environ.get("OAOS_EMBED_API_URL", "").strip()
    api_key = os.environ.get("OAOS_EMBED_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    if api_url or api_key:
        # In real deployment this would return an OpenAI-compatible provider.
        # We do not claim live external API in this skeleton: raise with guidance.
        raise RuntimeError(
            "Live embedding API configured but no real provider wired in this build. "
            "Inject a concrete EmbeddingProvider (e.g., FakeEmbeddingProvider for tests) "
            "explicitly. Set OAOS_EMBED_PROVIDER to select implementation."
        )
    if _is_production():
        raise RuntimeError(
            "No embedding provider configured in production. "
            "Set OAOS_EMBED_API_URL + OAOS_EMBED_API_KEY and provide a real provider. "
            "Hash/fake embeddings are blocked in production."
        )
    # non-prod: safe fallback for local dev is fake
    return FakeEmbeddingProvider(dim=dim)
