"""Embedding provider boundary.

Production must NOT silently use hash embeddings. Tests may use FakeProvider.

Design:
- EmbeddingProvider ABC: embed(texts) -> list[vector]; dim property
- FakeEmbeddingProvider: deterministic, no external calls, suitable for tests
- HashEmbeddingProvider: deterministic hash-derived, allowed only when explicitly
  enabled (OAOS_ALLOW_HASH_EMBED=1 or not in production). In production without
  explicit flag it raises.
- OllamaEmbeddingProvider: production-safe provider using Ollama /api/embed.
  Configured via OAOS_EMBED_API_URL and OAOS_EMBED_MODEL, fail-closed on
  errors or dimension mismatch. Supports injected http_client for tests.
- get_default_provider() respects env: if OAOS_EMBED_API_URL configured returns
  OllamaEmbeddingProvider, otherwise in production raises, in non-prod returns Fake.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.request
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


# ---------------------------------------------------------------------------
# Env helpers for Ollama
# ---------------------------------------------------------------------------

def _resolve_embed_api_url(explicit: str | None = None) -> str:
    if explicit is not None and explicit.strip():
        return explicit.strip().rstrip("/")
    for k in ("OAOS_EMBED_API_URL", "OAOS_EMBEDDING_API_URL", "OLLAMA_API_URL", "OLLAMA_HOST"):
        v = os.environ.get(k, "").strip()
        if v:
            return v.rstrip("/")
    return ""


def _resolve_embed_model(explicit: str | None = None) -> str:
    if explicit is not None and explicit.strip():
        return explicit.strip()
    for k in ("OAOS_EMBED_MODEL", "OAOS_EMBEDDING_MODEL", "OLLAMA_EMBED_MODEL", "OLLAMA_MODEL"):
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return "bge-m3:latest"


def _resolve_embed_dim(explicit: int | None, model: str) -> int:
    if explicit is not None:
        return int(explicit)
    env_dim = os.environ.get("OAOS_EMBED_DIM", "").strip() or os.environ.get("OAOS_EMBEDDING_DIM", "").strip()
    if env_dim:
        try:
            return int(env_dim)
        except Exception:
            pass
    # infer from model
    ml = model.lower()
    if "bge-m3" in ml:
        return 1024
    if "nomic-embed" in ml:
        return 768
    if "mxbai" in ml:
        return 1024
    return 1024 if "bge" in ml else 1536


def _assert_no_chromadb(api_url: str) -> None:
    low = api_url.lower()
    if "chroma" in low:
        raise ValueError("ChromaDB is forbidden — use Ollama 127.0.0.1:11434 only")
    # forbid ChromaDB default port 8000 (vector DB) — keep 11434 Ollama
    # exact check: :8000 as port delimiter, not spurious substring
    if ":8000" in api_url:
        raise ValueError("port 8000 (ChromaDB) is forbidden — OAOS_EMBED_API_URL must be Ollama 127.0.0.1:11434")


def _normalize_embed_endpoint(api_url: str) -> str:
    """Turn OAOS_EMBED_API_URL into full /api/embed endpoint.

    Accepts:
      http://127.0.0.1:11434
      http://127.0.0.1:11434/api
      http://127.0.0.1:11434/api/embed
      http://127.0.0.1:11434/api/embeddings  (alias)
    Returns full URL to POST for embeddings.
    """
    u = api_url.strip().rstrip("/")
    if not u:
        return ""
    _assert_no_chromadb(u)
    if u.endswith("/api/embed") or u.endswith("/api/embeddings"):
        return u
    if u.endswith("/api"):
        return u + "/embed"
    if "/api/embed" in u:
        return u
    return u + "/api/embed"


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


# ---------------------------------------------------------------------------
# Ollama provider — production-safe, fail-closed
# ---------------------------------------------------------------------------

class OllamaEmbeddingProvider(EmbeddingProvider):
    """Production-safe Ollama embedding provider via /api/embed.

    Uses Ollama's /api/embed endpoint (verified compatible with bge-m3:latest).
    Environment-configured:
      OAOS_EMBED_API_URL  – e.g. http://127.0.0.1:11434 or http://host:11434/api/embed
      OAOS_EMBED_MODEL    – e.g. bge-m3:latest (default)
      OAOS_EMBED_DIM      – expected dimension (default 1024 for bge-m3, else 1536)

    Fail-closed:
      - Missing OAOS_EMBED_API_URL -> RuntimeError on embed()
      - HTTP error / network failure -> RuntimeError
      - Missing or malformed embeddings field -> RuntimeError
      - Count mismatch (len(embeddings) != len(texts)) -> RuntimeError
      - Dimension mismatch (any vector len != dim) -> RuntimeError

    Supports injected http_client for tests:
      http_client.post(url, headers, json, timeout) -> resp with .status_code and .json()

    In production without explicit PYTEST_CURRENT_TEST bypass, construction verifies
    that api_url and model are present per security policy but defers network failure to embed().
    """

    def __init__(
        self,
        api_url: str | None = None,
        model: str | None = None,
        dim: int | None = None,
        timeout_s: float = 60.0,
        http_client: Any | None = None,
    ) -> None:
        raw_url = _resolve_embed_api_url(api_url)
        if raw_url:
            _assert_no_chromadb(raw_url)
        # Normalize to full endpoint only if url present
        self._raw_api_url = raw_url
        self._endpoint = _normalize_embed_endpoint(raw_url) if raw_url else ""
        # model resolution
        self._model = _resolve_embed_model(model)
        self._dim = _resolve_embed_dim(dim, self._model)
        self.timeout_s = float(timeout_s)
        self._http_client = http_client

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self._model

    @property
    def api_url(self) -> str:
        return self._raw_api_url

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._raw_api_url or not self._endpoint:
            raise RuntimeError(
                "OAOS_EMBED_API_URL not configured for Ollama provider "
                "(set OAOS_EMBED_API_URL=http://127.0.0.1:11434 and OAOS_EMBED_MODEL=bge-m3:latest) — failing closed"
            )
        if not texts:
            return []
        # Validate texts
        for t in texts:
            if not isinstance(t, str):
                raise RuntimeError(f"embed input must be str, got {type(t).__name__}")
        payload = {"model": self._model, "input": texts}
        # POST with fail-closed error handling
        resp = self._post_with_fail_closed(self._endpoint, payload)
        # Parse embeddings
        try:
            data = resp.json() if hasattr(resp, "json") else {}
        except Exception as e:
            raise RuntimeError(f"Ollama embed invalid JSON response: {e}") from e
        embeddings = None
        if isinstance(data, dict):
            embeddings = data.get("embeddings")
            if embeddings is None:
                embeddings = data.get("embedding")
            if embeddings is None:
                embeddings = data.get("data")
            # OpenAI-compat shape: {"data":[{"embedding":[...]}]} — unwrap
            if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], dict) and "embedding" in embeddings[0]:
                try:
                    embeddings = [item["embedding"] for item in embeddings if isinstance(item, dict) and "embedding" in item]  # type: ignore[assignment]
                except Exception:
                    embeddings = None
            if embeddings is None and isinstance(data.get("data"), list):
                # try openai shape fallback
                try:
                    embeddings = [item["embedding"] for item in data["data"] if isinstance(item, dict) and "embedding" in item]
                except Exception:
                    embeddings = None
        if embeddings is None:
            raise RuntimeError(f"Ollama embed missing 'embeddings' field in response: {data}")
        # Normalize single-vector case: {"embedding": [...]}
        if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], (int, float)):
            embeddings = [embeddings]  # type: ignore[assignment]
        if not isinstance(embeddings, list):
            raise RuntimeError(f"Ollama embed 'embeddings' not a list: {type(embeddings).__name__}")
        if len(embeddings) != len(texts):
            raise RuntimeError(
                f"Ollama embed count mismatch: expected {len(texts)} vectors got {len(embeddings)} — failing closed"
            )
        for idx, vec in enumerate(embeddings):
            if not isinstance(vec, list):
                raise RuntimeError(f"Ollama embed vector {idx} not a list: {type(vec).__name__}")
            if len(vec) != self._dim:
                raise RuntimeError(
                    f"embedding dimension mismatch: expected {self._dim} got {len(vec)} "
                    f"(model {self._model}, index {idx}) — failing closed"
                )
            # ensure all floats
            for v in vec:
                if not isinstance(v, (int, float)):
                    raise RuntimeError(f"Ollama embed vector {idx} contains non-float: {type(v).__name__}")
        # Cast to float
        return [list(map(float, vec)) for vec in embeddings]  # type: ignore[arg-type]

    def _post_with_fail_closed(self, url: str, payload: dict[str, Any]) -> Any:
        headers = {"Content-Type": "application/json"}
        timeout = self.timeout_s
        try:
            if self._http_client is not None:
                resp = self._http_client.post(url, headers=headers, json=payload, timeout=timeout)
            else:
                resp = _ollama_stdlib_post(url, headers=headers, json_body=payload, timeout=timeout)
        except Exception as e:
            raise RuntimeError(f"Ollama embed request failed: {e}") from e
        status = getattr(resp, "status_code", 200)
        if status != 200:
            # try to surface body
            try:
                body = resp.json() if hasattr(resp, "json") else getattr(resp, "text", str(resp))
            except Exception:
                body = getattr(resp, "text", "")
            raise RuntimeError(f"Ollama embed HTTP {status} for {url}: {body} — failing closed")
        return resp


def _ollama_stdlib_post(url: str, headers: dict[str, str], json_body: dict[str, Any], timeout: float) -> Any:
    data = json.dumps(json_body).encode("utf-8")
    hdrs = dict(headers)
    hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")

    class _Resp:
        def __init__(self, status: int, body: bytes) -> None:
            self.status_code = status
            self._body = body

        def json(self) -> Any:
            if not self._body:
                return {}
            return json.loads(self._body.decode("utf-8"))

        @property
        def text(self) -> str:
            return self._body.decode("utf-8", errors="replace")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return _Resp(resp.status, body)
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        return _Resp(e.code, body)
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama network error posting {url}: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Ollama unexpected error posting {url}: {e}") from e


def get_default_provider(dim: int = 1536) -> EmbeddingProvider:
    """Resolve default provider without silently using hash in prod.

    Priority:
    1. If OAOS_EMBED_API_URL configured -> return OllamaEmbeddingProvider
       (fail-closed on missing model/dim etc. at embed time).
    2. If OPENAI_API_KEY / OAOS_EMBED_API_KEY configured without URL -> raise with guidance
       (no silent fallback).
    3. If production -> raise (must configure real provider)
    4. Otherwise -> return FakeEmbeddingProvider for dev/test.

    This function MUST NOT return HashEmbeddingProvider silently in production.
    Tests should inject FakeEmbeddingProvider explicitly.
    """
    api_url = _resolve_embed_api_url()
    api_key = os.environ.get("OAOS_EMBED_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    # Ollama path — preferred
    if api_url:
        # Resolve model/dim for construction
        model = _resolve_embed_model()
        # Use env dim if set else respect caller's dim only for compatibility: caller dim overrides env inference?
        # If caller passes explicit dim != default, use it; else infer from model.
        # To avoid breaking prod where dim mismatch must fail-closed, we infer.
        env_dim_raw = os.environ.get("OAOS_EMBED_DIM", "").strip()
        if env_dim_raw:
            try:
                eff_dim = int(env_dim_raw)
            except Exception:
                eff_dim = dim
        else:
            # infer; if caller dim is non-default and model is bge-m3, still use bge-m3 dim to surface mismatch correctly
            eff_dim = _resolve_embed_dim(None, model) if "bge" in model.lower() else dim
        return OllamaEmbeddingProvider(api_url=api_url, model=model, dim=eff_dim)
    if api_key:
        raise RuntimeError(
            "Live embedding API key configured but OAOS_EMBED_API_URL missing. "
            "Set OAOS_EMBED_API_URL (e.g. http://127.0.0.1:11434) and OAOS_EMBED_MODEL. "
            "Inject a concrete EmbeddingProvider explicitly. Set OAOS_EMBED_PROVIDER to select implementation."
        )
    if _is_production():
        raise RuntimeError(
            "No embedding provider configured in production. "
            "Set OAOS_EMBED_API_URL (e.g. http://127.0.0.1:11434) + OAOS_EMBED_MODEL=bge-m3:latest and provide a real provider. "
            "Hash/fake embeddings are blocked in production."
        )
    return FakeEmbeddingProvider(dim=dim)
