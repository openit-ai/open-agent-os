"""Personal Wiki pgvector embedding integration — lazy, no hard deps.

- Takes vault file path, reads text, chunks, embeds, writes to oaos memories table via asyncpg/SQLAlchemy or mock.
- Embedding chain: remote memory_service/openai API (if OAOS_EMBED_API_URL+key) -> sentence-transformers local (if installed) -> deterministic hash embedding (always works)
- Chunking: char-based with overlap (default 800/100), configurable via env.
- DB write: tries SQLAlchemy async (postgres/sqlite) then asyncpg raw, else returns mock result. Never raises at import time.

Env:
  OAOS_EMBED_ENABLED=1|true -> enable auto-embed from vault.py
  OAOS_EMBED_CHUNK_SIZE=800
  OAOS_EMBED_CHUNK_OVERLAP=100
  OAOS_EMBED_DIM=1536
  OAOS_EMBED_API_URL=http://memory-service:8100/v1/embeddings (optional remote)
  OAOS_EMBED_API_KEY / OPENAI_API_KEY -> for remote embedding
  OAOS_EMBED_MODEL=text-embedding-3-small (remote)
  DATABASE_URL / OAOS_DATABASE_URL -> DB write; when unset -> mock

All imports are lazy inside functions so `import personal_wiki.embed` never fails when deps missing.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
from datetime import UTC, datetime
from numbers import Real
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    from sqlalchemy.exc import SQLAlchemyError
except (ImportError, ModuleNotFoundError):  # sqlalchemy is lazy/optional; best-effort fallback
    class SQLAlchemyErrorFallback(Exception):
        """Marker used when SQLAlchemy is not installed."""

    SQLAlchemyError = SQLAlchemyErrorFallback  # type: ignore[misc,assignment]

DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 100
DEFAULT_DIM = 1536


class EmbeddingError(RuntimeError):
    """Base error for provider and durable embedding failures."""

    status_code = 503


class EmbeddingConfigurationError(ValueError):
    """Embedding configuration is malformed or incomplete."""

    status_code = 422


class EmbeddingResponseError(EmbeddingError):
    """A provider returned a response that cannot be used as a vector."""

    status_code = 502


class EmbeddingRateLimitError(EmbeddingError):
    """The embedding provider rejected a request due to rate limiting."""

    status_code = 429


class EmbeddingProviderError(EmbeddingError):
    """The configured embedding provider is unavailable or failed."""


class EmbeddingStorageError(EmbeddingError):
    """Durable memory storage is unavailable or rejected the write."""


def _is_production() -> bool:
    return any(
        os.getenv(key, "").strip().lower() in ("production", "prod")
        for key in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT")
    )


def _allow_nonprod_fallback() -> bool:
    """Allow deterministic/mock fallback only for an explicit non-prod fixture."""
    if _is_production():
        return False
    return any(
        os.getenv(key, "").strip().lower() in ("1", "true", "yes", "on")
        for key in (
            "OAOS_ALLOW_EMBED_FALLBACK",
            "OAOS_ALLOW_TEST_FIXTURE",
            "OAOS_ALLOW_TEST_FALLBACK",
        )
    ) or bool(os.getenv("PYTEST_CURRENT_TEST"))


def _fallback_warning(reason: str) -> None:
    logger.warning("personal wiki embedding degraded to deterministic/mock fallback: %s", reason)


def _parse_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise EmbeddingConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise EmbeddingConfigurationError(f"{name} must be greater than zero")
    return value


def _resolve_dim(dim: int | None = None) -> int:
    if dim is None:
        return _parse_env_int("OAOS_EMBED_DIM", DEFAULT_DIM)
    if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
        raise EmbeddingConfigurationError("embedding dimension must be a positive integer")
    return dim


def _validate_vectors(
    vectors: list[list[float]],
    texts: list[str],
    dim: int,
    source: str,
) -> list[list[float]]:
    if len(vectors) != len(texts):
        raise EmbeddingResponseError(
            f"{source} returned {len(vectors)} vectors for {len(texts)} inputs"
        )
    validated: list[list[float]] = []
    for index, vector in enumerate(vectors):
        if not isinstance(vector, list) or len(vector) != dim:
            raise EmbeddingResponseError(
                f"{source} vector {index} has invalid dimension"
            )
        if any(
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not math.isfinite(float(value))
            for value in vector
        ):
            raise EmbeddingResponseError(f"{source} vector {index} has invalid numeric values")
        validated.append([float(value) for value in vector])
    return validated


def is_embed_enabled() -> bool:
    v = os.getenv("OAOS_EMBED_ENABLED", "0")
    return v.strip().lower() in ("1", "true", "yes", "on")


def _dim() -> int:
    return _parse_env_int("OAOS_EMBED_DIM", DEFAULT_DIM)


def _chunk_size() -> int:
    return _parse_env_int("OAOS_EMBED_CHUNK_SIZE", DEFAULT_CHUNK_SIZE)


def _overlap() -> int:
    raw = os.getenv("OAOS_EMBED_CHUNK_OVERLAP")
    if raw is None or not raw.strip():
        return DEFAULT_CHUNK_OVERLAP
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise EmbeddingConfigurationError("OAOS_EMBED_CHUNK_OVERLAP must be an integer") from exc
    if value < 0:
        raise EmbeddingConfigurationError("OAOS_EMBED_CHUNK_OVERLAP must not be negative")
    return value


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_text(text: str, chunk_size: int | None = None, overlap: int | None = None) -> list[str]:
    """Split text into overlapping char chunks. Always returns at least 1 chunk for non-empty text."""
    if not text:
        return []
    cs = chunk_size if chunk_size is not None else _chunk_size()
    ov = overlap if overlap is not None else _overlap()
    # clamp
    cs = max(64, cs)
    ov = max(0, min(ov, cs - 1))
    if len(text) <= cs:
        return [text]
    chunks: list[str] = []
    step = cs - ov
    for i in range(0, len(text), step):
        c = text[i : i + cs]
        if not c.strip():
            continue
        chunks.append(c)
        if i + cs >= len(text):
            break
    return chunks or [text[:cs]]


# ---------------------------------------------------------------------------
# Hash embedding (deterministic fallback, no deps)
# ---------------------------------------------------------------------------

def hash_embedding(text: str, dim: int | None = None) -> list[float]:
    """Deterministic hash embedding in dim — L2-normalized, no external deps."""
    d = _resolve_dim(dim)
    # Use SHA256 in counter mode to generate enough bytes
    out: list[float] = []
    counter = 0
    # hash text once to seed
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    while len(out) < d:
        h = hashlib.sha256(seed + counter.to_bytes(4, "little")).digest()
        # each byte -> float in [-1,1]
        for b in h:
            # map 0..255 -> -1..1
            v = (b / 127.5) - 1.0
            out.append(v)
            if len(out) >= d:
                break
        counter += 1
    # L2 normalize
    norm = math.sqrt(sum(x * x for x in out))
    if norm > 0:
        out = [x / norm for x in out]
    return out


# ---------------------------------------------------------------------------
# Local embedding — sentence-transformers if available else hash
# ---------------------------------------------------------------------------

def _try_sentence_transformer(texts: list[str], dim: int) -> list[list[float]] | None:
    """Use the optional local model, returning ``None`` only when unavailable."""
    import importlib.util

    try:
        if importlib.util.find_spec("sentence_transformers") is None:
            return None
        from sentence_transformers import SentenceTransformer  # type: ignore
    except (ImportError, ModuleNotFoundError):
        return None

    model_name = os.getenv("OAOS_EMBED_MODEL_LOCAL", "sentence-transformers/all-MiniLM-L6-v2")
    try:
        model = SentenceTransformer(model_name)  # type: ignore
        embs = model.encode(texts, normalize_embeddings=True)  # type: ignore
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise EmbeddingProviderError("local embedding model unavailable") from exc

    try:
        if hasattr(embs, "tolist"):
            raw_vectors = embs.tolist()  # type: ignore
        else:
            raw_vectors = [list(row) for row in embs]  # type: ignore
    except (AttributeError, TypeError, ValueError) as exc:
        raise EmbeddingResponseError("local embedding model returned malformed vectors") from exc
    return _validate_vectors(raw_vectors, texts, dim, "local embedding model")


def _try_remote_embedding(texts: list[str], dim: int) -> list[list[float]] | None:
    """Call the configured remote provider; ``None`` means it is not configured."""
    api_url = os.getenv("OAOS_EMBED_API_URL", "").strip()
    api_key = os.getenv("OAOS_EMBED_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("OAOS_EMBED_MODEL", "text-embedding-3-small")
    if not api_url or not api_key:
        if not api_key:
            return None
        api_url = api_url or "https://api.openai.com/v1/embeddings"
    try:
        import httpx  # type: ignore
    except (ImportError, ModuleNotFoundError) as exc:
        raise EmbeddingProviderError("remote embedding client unavailable") from exc

    payload = {"model": model, "input": texts}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(api_url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise EmbeddingProviderError("remote embedding provider timed out") from exc
    except httpx.NetworkError as exc:
        raise EmbeddingProviderError("remote embedding provider is unavailable") from exc
    except httpx.HTTPError as exc:
        raise EmbeddingProviderError("remote embedding transport failed") from exc

    if resp.status_code == 429:
        raise EmbeddingRateLimitError("remote embedding provider rate limited the request")
    if resp.status_code != 200:
        raise EmbeddingProviderError(
            f"remote embedding provider returned HTTP {resp.status_code}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise EmbeddingResponseError("remote embedding response was not valid JSON") from exc

    if not isinstance(data, dict):
        raise EmbeddingResponseError("remote embedding response must be an object")
    if "data" in data:
        raw_items = data["data"]
        if not isinstance(raw_items, list):
            raise EmbeddingResponseError("remote embedding data must be a list")
        vectors: list[list[float]] = []
        for item in raw_items:
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise EmbeddingResponseError("remote embedding item is malformed")
            vectors.append(item["embedding"])
        return _validate_vectors(vectors, texts, dim, "remote embedding provider")
    if "embeddings" in data:
        raw_vectors = data["embeddings"]
        if not isinstance(raw_vectors, list):
            raise EmbeddingResponseError("remote embeddings must be a list")
        return _validate_vectors(raw_vectors, texts, dim, "remote embedding provider")
    raise EmbeddingResponseError("remote embedding response did not contain vectors")


def get_embedding(text: str, dim: int | None = None) -> list[float]:
    """Return one vector, failing closed when no real provider is available in production."""
    d = _resolve_dim(dim)
    texts = [text]
    try:
        vecs = _try_remote_embedding(texts, d)
        if vecs is not None:
            return vecs[0]
        vecs = _try_sentence_transformer(texts, d)
        if vecs is not None:
            return vecs[0]
    except (EmbeddingError, EmbeddingConfigurationError) as exc:
        if not _allow_nonprod_fallback():
            raise
        _fallback_warning(str(exc))
    if _allow_nonprod_fallback():
        _fallback_warning("no embedding provider configured")
        return hash_embedding(text, dim=d)
    raise EmbeddingProviderError("no embedding provider configured")


def get_embeddings(texts: list[str], dim: int | None = None) -> list[list[float]]:
    """Return batch vectors with strict count, dimension, and numeric validation."""
    if not texts:
        return []
    d = _resolve_dim(dim)
    try:
        vecs = _try_remote_embedding(texts, d)
        if vecs is not None:
            return _validate_vectors(vecs, texts, d, "remote embedding provider")
        vecs = _try_sentence_transformer(texts, d)
        if vecs is not None:
            return _validate_vectors(vecs, texts, d, "local embedding model")
    except (EmbeddingError, EmbeddingConfigurationError) as exc:
        if not _allow_nonprod_fallback():
            raise
        _fallback_warning(str(exc))
    if _allow_nonprod_fallback():
        _fallback_warning("embedding provider unavailable")
        return [hash_embedding(text, dim=d) for text in texts]
    raise EmbeddingProviderError("no embedding provider configured")


# ---------------------------------------------------------------------------
# DB write — asyncpg / SQLAlchemy or mock
# ---------------------------------------------------------------------------

def _db_url() -> str | None:
    url = os.getenv("DATABASE_URL") or os.getenv("OAOS_DATABASE_URL") or ""
    return url.strip() or None


def _is_db_configured() -> bool:
    return bool(_db_url())


async def _write_via_sqlalchemy(
    chunks: list[str],
    embeddings: list[list[float]],
    source_path: Path,
    metadata: dict[str, Any] | None,
    owner: str = "employee:anonymous",
    tenant_id: str = "default",
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Try SQLAlchemy async write to memories table. Returns dict or raises to trigger fallback."""
    # lazy imports
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # type: ignore
    except (ImportError, ModuleNotFoundError) as e:
        raise RuntimeError(f"sqlalchemy not available: {e}")

    url = _db_url()
    if not url:
        raise RuntimeError("no DATABASE_URL")

    # normalize url for async
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        url = url.replace("sqlite://", "sqlite+aiosqlite://", 1)

    # lazy ORM import
    try:
        from security.models.db import Base  # type: ignore
        from security.models.orm import MemoryORM, MemorySourceORM  # type: ignore
    except (ImportError, ModuleNotFoundError):
        # fallback path
        import sys

        root = Path(__file__).resolve().parents[3]
        sec = str(root / "security")
        if sec not in sys.path:
            sys.path.insert(0, sec)
        try:
            from security.models.db import Base  # type: ignore
            from security.models.orm import MemoryORM, MemorySourceORM  # type: ignore
        except (ImportError, ModuleNotFoundError) as e:
            raise RuntimeError(f"ORM import failed: {e}")

    # ensure aiosqlite / asyncpg driver available
    engine = create_async_engine(url, echo=False, pool_pre_ping=True)
    # create tables lazily for sqlite/tests
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except SQLAlchemyError as exc:
        raise EmbeddingStorageError("embedding storage schema is unavailable") from exc

    maker = async_sessionmaker(engine, expire_on_commit=False)

    if not agent_id:
        agent_id = owner.replace("employee:", "agent:assistant:") if "employee:" in owner else f"agent:assistant:{owner}"

    import json as _json
    import uuid

    now = datetime.now(UTC)
    meta = metadata or {}
    # detect Text fallback for embedding column
    try:
        from security.models.orm import _VECTOR_1536 as _vec  # type: ignore
        from sqlalchemy import Text as _SA_Text  # type: ignore

        is_text = _vec is _SA_Text or str(_vec) == "TEXT"
    except (ImportError, AttributeError, TypeError, ValueError):
        is_text = True

    created_ids: list[str] = []
    try:
        async with maker() as session:
            for idx, (chunk, emb) in enumerate(zip(chunks, embeddings)):
                mid = f"mem_{uuid.uuid4().hex[:12]}"
                # handle embedding serialization for Text fallback
                emb_val: Any = emb
                if is_text:
                    try:
                        emb_val = _json.dumps(emb)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise EmbeddingResponseError("embedding vector serialization failed") from exc
                # derive owner fields
                if owner.startswith("employee:"):
                    owner_type, owner_id = "employee", owner.split(":", 1)[1]
                elif owner.startswith("group:"):
                    owner_type, owner_id = "group", owner.split(":", 1)[1]
                else:
                    owner_type, owner_id = "unknown", owner
                mem = MemoryORM(  # type: ignore
                    id=mid,
                    tenant_id=tenant_id,
                    user_id=owner,
                    agent_id=agent_id,
                    kind="personal_wiki",
                    content=chunk,
                    embedding=emb_val,
                    source_ids=[str(source_path)],
                    created_at=now,
                    updated_at=now,
                )
                # Phase B columns (best-effort setattr)
                for col, val in [
                    ("namespace", meta.get("namespace")),
                    ("owner_type", owner_type),
                    ("owner_id", owner_id),
                    ("memory_type", "personal_wiki"),
                    ("classification", meta.get("classification", "INTERNAL")),
                    ("retention_policy", "standard"),
                    ("summary", chunk[:200]),
                    ("source_resource_type", "personal_wiki"),
                    ("source_resource_id", str(source_path)),
                ]:
                    if hasattr(mem, col):
                        try:
                            setattr(mem, col, val)
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.debug("optional memory column assignment skipped: %s", type(exc).__name__)
                session.add(mem)
                # source provenance row
                src = MemorySourceORM(  # type: ignore
                    id=f"ms_{uuid.uuid4().hex[:12]}",
                    tenant_id=tenant_id,
                    memory_id=mid,
                    source_type="personal_wiki",
                    source_id=str(source_path),
                    source_uri=str(source_path),
                    metadata_=meta,
                    created_at=now,
                )
                session.add(src)
                created_ids.append(mid)
            await session.commit()
    except (SQLAlchemyError, EmbeddingError) as e:
        try:
            await engine.dispose()
        except SQLAlchemyError:
            logger.debug("embed engine dispose failed (best-effort)")
        if isinstance(e, EmbeddingError):
            raise
        raise EmbeddingStorageError("sqlalchemy embedding write failed") from e
    try:
        await engine.dispose()
    except SQLAlchemyError:
        logger.debug("embed engine dispose failed (best-effort)")
    return {"mock": False, "inserted": len(created_ids), "ids": created_ids, "chunks": len(chunks)}


async def _write_via_memory_service_api(
    chunks: list[str],
    embeddings: list[list[float]],
    source_path: Path,
    metadata: dict[str, Any] | None,
    owner: str = "employee:anonymous",
    tenant_id: str = "default",
    agent_id: str | None = None,
) -> dict[str, Any] | None:
    """POST chunks to memory_service when explicitly configured."""
    svc_url = os.getenv("OAOS_MEMORY_SERVICE_URL", "") or os.getenv("MEMORY_SERVICE_URL", "")
    if not svc_url:
        return None
    try:
        import httpx  # type: ignore
    except (ImportError, ModuleNotFoundError) as exc:
        raise EmbeddingStorageError("memory service HTTP client unavailable") from exc

    base = svc_url.rstrip("/")
    inserted = 0
    ids: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for chunk, emb in zip(chunks, embeddings):
                payload = {
                    "content": chunk,
                    "owner": owner,
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "classification": (metadata or {}).get("classification", "INTERNAL"),
                    "source_resource_id": str(source_path),
                    "provenance": metadata or {"source": "personal_wiki", "vault_path": str(source_path)},
                    "embedding": emb,
                    "scope": "personal",
                }
                headers = {"X-User-Id": owner, "X-Tenant-Id": tenant_id}
                if agent_id:
                    headers["X-Agent-Id"] = agent_id
                resp = await client.post(f"{base}/v1/memory/write", json=payload, headers=headers)
                if resp.status_code == 429:
                    raise EmbeddingRateLimitError("memory service rate limited the write")
                if resp.status_code not in (200, 201):
                    raise EmbeddingStorageError(
                        f"memory service write failed after {inserted} chunks: HTTP {resp.status_code}"
                    )
                try:
                    response_data = resp.json()
                except ValueError as exc:
                    raise EmbeddingResponseError("memory service write response was not valid JSON") from exc
                if not isinstance(response_data, dict):
                    raise EmbeddingResponseError("memory service write response must be an object")
                memory_id = response_data.get("id") or response_data.get("memory_id")
                if not isinstance(memory_id, str) or not memory_id:
                    raise EmbeddingResponseError("memory service write response omitted memory id")
                ids.append(memory_id)
                inserted += 1
    except httpx.TimeoutException as exc:
        raise EmbeddingStorageError("memory service write timed out") from exc
    except httpx.NetworkError as exc:
        raise EmbeddingStorageError("memory service is unavailable") from exc
    except httpx.HTTPError as exc:
        raise EmbeddingStorageError("memory service transport failed") from exc
    return {"mock": False, "inserted": inserted, "ids": ids, "chunks": len(chunks), "via": "memory_service_api"}


async def embed_text(
    content: str,
    source_path: Path | str = "vault_note",
    metadata: dict[str, Any] | None = None,
    owner: str = "employee:anonymous",
    tenant_id: str = "default",
    agent_id: str | None = None,
    chunk_size: int | None = None,
    overlap: int | None = None,
    dim: int | None = None,
) -> dict[str, Any]:
    """Chunk, embed, and persist text; production failures are never mock-successes."""
    if not content or not content.strip():
        return {"mock": True, "inserted": 0, "chunks": 0, "reason": "empty content", "source_path": str(source_path)}
    cs = _chunk_size() if chunk_size is None else chunk_size
    ov = _overlap() if overlap is None else overlap
    d = _resolve_dim(dim)
    if not isinstance(cs, int) or isinstance(cs, bool) or cs <= 0:
        raise EmbeddingConfigurationError("chunk_size must be a positive integer")
    if not isinstance(ov, int) or isinstance(ov, bool) or ov < 0:
        raise EmbeddingConfigurationError("overlap must be a non-negative integer")
    chunks = chunk_text(content, chunk_size=cs, overlap=ov)
    if not chunks:
        return {"mock": True, "inserted": 0, "chunks": 0, "reason": "no chunks", "source_path": str(source_path)}
    embeddings = get_embeddings(chunks, dim=d)
    sp = Path(source_path)
    meta = dict(metadata or {})
    meta.setdefault("source", "personal_wiki")
    meta.setdefault("vault_path", str(sp))
    # Priority: 1) memory_service HTTP API if configured, 2) direct DB via SQLAlchemy.
    api_failure: EmbeddingError | None = None
    if os.getenv("OAOS_MEMORY_SERVICE_URL", "").strip() or os.getenv("MEMORY_SERVICE_URL", "").strip():
        try:
            api_res = await _write_via_memory_service_api(
                chunks, embeddings, sp, meta, owner=owner, tenant_id=tenant_id, agent_id=agent_id
            )
            if api_res is not None:
                return api_res
        except EmbeddingError as exc:
            api_failure = exc
            logger.warning("memory service embedding write degraded: %s", type(exc).__name__)
    if _is_db_configured():
        try:
            return await _write_via_sqlalchemy(chunks, embeddings, sp, meta, owner=owner, tenant_id=tenant_id, agent_id=agent_id)
        except EmbeddingError as exc:
            logger.warning("durable embedding write failed: %s", type(exc).__name__)
            if not _allow_nonprod_fallback():
                raise
            api_failure = exc
    if not _allow_nonprod_fallback():
        if api_failure is not None:
            raise api_failure
        raise EmbeddingStorageError("durable embedding storage is not configured")
    reason = "no durable embedding backend configured"
    if api_failure is not None:
        reason = f"durable embedding write failed: {type(api_failure).__name__}"
    _fallback_warning(reason)
    return {
        "mock": True,
        "inserted": len(chunks),
        "chunks": len(chunks),
        "ids": [f"mock_{i}" for i in range(len(chunks))],
        "source_path": str(sp),
        "reason": reason,
        "degraded": True,
        "embeddings": embeddings if len(chunks) <= 5 else embeddings[:1],  # avoid huge payload
    }


async def embed_file(
    file_path: Path | str,
    metadata: dict[str, Any] | None = None,
    owner: str = "employee:anonymous",
    tenant_id: str = "default",
    agent_id: str | None = None,
    max_chars: int = 20000,
    chunk_size: int | None = None,
    overlap: int | None = None,
    dim: int | None = None,
) -> dict[str, Any]:
    """Read vault file, chunk, embed, write to memories."""
    p = Path(file_path)
    try:
        exists = p.exists()
    except OSError as exc:
        raise EmbeddingStorageError("embedding source filesystem is unavailable") from exc
    if not exists:
        return {"mock": True, "inserted": 0, "chunks": 0, "reason": "file not found", "source_path": str(p)}
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = p.read_text(encoding="latin-1")
        except OSError as exc:
            raise EmbeddingStorageError("embedding source file could not be read") from exc
    except OSError as exc:
        raise EmbeddingStorageError("embedding source file could not be read") from exc
    if len(text) > max_chars:
        text = text[:max_chars]
    return await embed_text(
        text,
        source_path=p,
        metadata=metadata,
        owner=owner,
        tenant_id=tenant_id,
        agent_id=agent_id,
        chunk_size=chunk_size,
        overlap=overlap,
        dim=dim,
    )


def embed_file_sync(
    file_path: Path | str,
    metadata: dict[str, Any] | None = None,
    owner: str = "employee:anonymous",
    tenant_id: str = "default",
    agent_id: str | None = None,
    max_chars: int = 20000,
    chunk_size: int | None = None,
    overlap: int | None = None,
    dim: int | None = None,
) -> dict[str, Any]:
    """Sync wrapper for embed_file — safe to call from vault.py (non-async)."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        if not _allow_nonprod_fallback():
            raise EmbeddingProviderError("synchronous embedding cannot run inside an active event loop")
        p = Path(file_path)
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_text(encoding="latin-1")
        except OSError as exc:
            raise EmbeddingStorageError("embedding source file could not be read") from exc
        if len(text) > max_chars:
            text = text[:max_chars]
        chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        embeddings = get_embeddings(chunks, dim=dim)
        _fallback_warning("running event loop deferred durable embedding write")
        return {
            "mock": True,
            "inserted": len(chunks),
            "chunks": len(chunks),
            "ids": [f"mock_{i}" for i in range(len(chunks))],
            "source_path": str(p),
            "reason": "running loop — sync mock (embed deferred)",
            "degraded": True,
            "embeddings": embeddings[:1] if embeddings else [],
        }
    return asyncio.run(
        embed_file(
            file_path,
            metadata=metadata,
            owner=owner,
            tenant_id=tenant_id,
            agent_id=agent_id,
            max_chars=max_chars,
            chunk_size=chunk_size,
            overlap=overlap,
            dim=dim,
        )
    )


def embed_text_sync(
    content: str,
    source_path: Path | str = "vault_note",
    metadata: dict[str, Any] | None = None,
    owner: str = "employee:anonymous",
    tenant_id: str = "default",
    agent_id: str | None = None,
    chunk_size: int | None = None,
    overlap: int | None = None,
    dim: int | None = None,
) -> dict[str, Any]:
    """Sync wrapper for embed_text."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        if not _allow_nonprod_fallback():
            raise EmbeddingProviderError("synchronous embedding cannot run inside an active event loop")
        chunks = chunk_text(content, chunk_size=chunk_size, overlap=overlap)
        d = _resolve_dim(dim)
        embeddings = get_embeddings(chunks, dim=d)
        _fallback_warning("running event loop deferred durable embedding write")
        return {
            "mock": True,
            "inserted": len(chunks),
            "chunks": len(chunks),
            "ids": [f"mock_{i}" for i in range(len(chunks))],
            "source_path": str(source_path),
            "reason": "running loop — sync mock",
            "degraded": True,
            "embeddings": embeddings[:1] if embeddings else [],
        }
    return asyncio.run(
        embed_text(content, source_path, metadata, owner, tenant_id, agent_id, chunk_size, overlap, dim)
    )
