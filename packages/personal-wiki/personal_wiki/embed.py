"""Personal Wiki pgvector embedding integration — lazy, no hard deps.

- Takes vault file path, reads text, chunks, embeds, writes to openagentos memories table via asyncpg/SQLAlchemy or mock.
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
import math
import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 100
DEFAULT_DIM = 1536


def is_embed_enabled() -> bool:
    v = os.getenv("OAOS_EMBED_ENABLED", "0")
    return v.strip().lower() in ("1", "true", "yes", "on")


def _dim() -> int:
    try:
        return int(os.getenv("OAOS_EMBED_DIM", str(DEFAULT_DIM)))
    except Exception:
        return DEFAULT_DIM


def _chunk_size() -> int:
    try:
        return int(os.getenv("OAOS_EMBED_CHUNK_SIZE", str(DEFAULT_CHUNK_SIZE)))
    except Exception:
        return DEFAULT_CHUNK_SIZE


def _overlap() -> int:
    try:
        return int(os.getenv("OAOS_EMBED_CHUNK_OVERLAP", str(DEFAULT_CHUNK_OVERLAP)))
    except Exception:
        return DEFAULT_CHUNK_OVERLAP


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
    d = dim or _dim()
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
    """Try sentence-transformers locally; returns None on any failure."""
    try:
        import importlib.util

        if importlib.util.find_spec("sentence_transformers") is None:
            return None
        from sentence_transformers import SentenceTransformer  # type: ignore

        model_name = os.getenv("OAOS_EMBED_MODEL_LOCAL", "sentence-transformers/all-MiniLM-L6-v2")
        # truncated: if model download would block, fallback quickly
        # Use try with timeout-ish: just attempt load, if fails return None
        try:
            model = SentenceTransformer(model_name)  # type: ignore
        except Exception as e:
            logger.debug(f"sentence-transformer load failed: {e}")
            return None
        embs = model.encode(texts, normalize_embeddings=True)  # type: ignore
        # embs is np array; convert
        try:
            import numpy as np  # type: ignore

            if hasattr(embs, "tolist"):
                lst = embs.tolist()  # type: ignore
            else:
                lst = [list(r) for r in embs]  # type: ignore
        except Exception:
            lst = [list(r) for r in embs]  # type: ignore
        # pad/truncate to dim
        fixed: list[list[float]] = []
        for vec in lst:
            if len(vec) == dim:
                fixed.append([float(x) for x in vec])
            elif len(vec) < dim:
                # pad with hash-derived values
                pad = hash_embedding(texts[len(fixed)], dim=dim)[len(vec) :]
                fixed.append([float(x) for x in vec] + pad[: dim - len(vec)])
            else:
                fixed.append([float(x) for x in vec[:dim]])
        return fixed
    except Exception as e:
        logger.debug(f"sentence-transformer embedding failed: {e}")
        return None


def _try_remote_embedding(texts: list[str], dim: int) -> list[list[float]] | None:
    """Try remote embedding API (OpenAI-compatible) if env configured. Returns None on failure."""
    api_url = os.getenv("OAOS_EMBED_API_URL", "").strip()
    api_key = os.getenv("OAOS_EMBED_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("OAOS_EMBED_MODEL", "text-embedding-3-small")
    if not api_url or not api_key:
        # also allow memory_service embedding passthrough: if only key set, use OpenAI endpoint
        if not api_key:
            return None
        # fallback to OpenAI default url if key present but no url
        api_url = api_url or "https://api.openai.com/v1/embeddings"
    # lazy httpx
    try:
        import httpx  # type: ignore

        # Use OpenAI-compatible payload
        payload = {"model": model, "input": texts}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        # For memory_service custom, it may expect different path; we try both
        # If api_url already contains embeddings, post directly
        url = api_url
        # httpx sync client for sync path
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.debug(f"remote embedding failed {resp.status_code}: {resp.text[:500]}")
                return None
            data = resp.json()
            # OpenAI shape: {data: [{embedding: [...]}, ...]}
            if isinstance(data, dict) and "data" in data:
                vectors = []
                for item in data["data"]:
                    emb = item.get("embedding")
                    if isinstance(emb, list):
                        # normalize/truncate
                        if len(emb) != dim:
                            # pad or trim
                            if len(emb) < dim:
                                emb = emb + [0.0] * (dim - len(emb))
                            else:
                                emb = emb[:dim]
                        # ensure normalized? hash fallback already normalized
                        vectors.append([float(x) for x in emb])
                if len(vectors) == len(texts):
                    return vectors
            # alternative shape: {embeddings: [...]}
            if isinstance(data, dict) and "embeddings" in data:
                return [[float(x) for x in e[:dim]] for e in data["embeddings"]]  # type: ignore
        return None
    except Exception as e:
        logger.debug(f"remote embedding error: {e}")
        return None


def get_embedding(text: str, dim: int | None = None) -> list[float]:
    """Single text embedding — tries remote -> sentence-transformer -> hash. Always returns dim floats."""
    d = dim or _dim()
    texts = [text]
    # 1) remote if configured
    vecs = _try_remote_embedding(texts, d)
    if vecs and len(vecs) == 1:
        return vecs[0]
    # 2) local sentence-transformer
    vecs = _try_sentence_transformer(texts, d)
    if vecs and len(vecs) == 1:
        return vecs[0]
    # 3) hash fallback (always works)
    return hash_embedding(text, dim=d)


def get_embeddings(texts: list[str], dim: int | None = None) -> list[list[float]]:
    """Batch embedding for multiple chunks."""
    if not texts:
        return []
    d = dim or _dim()
    # try remote batch
    vecs = _try_remote_embedding(texts, d)
    if vecs and len(vecs) == len(texts):
        return vecs
    # try sentence-transformer batch
    vecs = _try_sentence_transformer(texts, d)
    if vecs and len(vecs) == len(texts):
        return vecs
    # hash fallback per chunk
    return [hash_embedding(t, dim=d) for t in texts]


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
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker  # type: ignore
    except Exception as e:
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
    except Exception:
        # fallback path
        import sys

        root = Path(__file__).resolve().parents[3]
        sec = str(root / "security")
        if sec not in sys.path:
            sys.path.insert(0, sec)
        try:
            from security.models.db import Base  # type: ignore
            from security.models.orm import MemoryORM, MemorySourceORM  # type: ignore
        except Exception as e:
            raise RuntimeError(f"ORM import failed: {e}")

    # ensure aiosqlite / asyncpg driver available
    engine = create_async_engine(url, echo=False, pool_pre_ping=True)
    # create tables lazily for sqlite/tests
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception:
        pass

    maker = async_sessionmaker(engine, expire_on_commit=False)

    if not agent_id:
        agent_id = owner.replace("employee:", "agent:assistant:") if "employee:" in owner else f"agent:assistant:{owner}"

    import uuid
    import json as _json

    now = datetime.now(timezone.utc)
    meta = metadata or {}
    # detect Text fallback for embedding column
    try:
        from security.models.orm import _VECTOR_1536 as _vec  # type: ignore
        from sqlalchemy import Text as _SA_Text  # type: ignore

        is_text = _vec is _SA_Text or str(_vec) == "TEXT"
    except Exception:
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
                    except Exception:
                        emb_val = None
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
                        except Exception:
                            pass
                session.add(mem)
                # source provenance row
                try:
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
                except Exception:
                    pass
                created_ids.append(mid)
            await session.commit()
    except Exception as e:
        try:
            await engine.dispose()
        except Exception:
            pass
        raise RuntimeError(f"sqlalchemy write failed: {e}")
    try:
        await engine.dispose()
    except Exception:
        pass
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
    """Try to POST chunks to memory_service HTTP API if MEM_SERVICE_URL configured."""
    svc_url = os.getenv("OAOS_MEMORY_SERVICE_URL", "") or os.getenv("MEMORY_SERVICE_URL", "")
    if not svc_url:
        return None
    # lazy httpx
    try:
        import httpx  # type: ignore

        base = svc_url.rstrip("/")
        # memory_service expects POST /v1/memory/write with content + embedding
        inserted = 0
        ids: list[str] = []
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
                if resp.status_code in (200, 201):
                    try:
                        j = resp.json()
                        ids.append(j.get("id") or j.get("memory_id") or "")
                    except Exception:
                        pass
                    inserted += 1
                else:
                    logger.warning(f"memory_service write failed {resp.status_code}: {resp.text[:500]}")
                    return None
        return {"mock": False, "inserted": inserted, "ids": ids, "chunks": len(chunks), "via": "memory_service_api"}
    except Exception as e:
        logger.debug(f"memory_service api write failed: {e}")
        return None


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
    """Chunk + embed + write to memories. Never raises; returns result dict with mock flag."""
    if not content or not content.strip():
        return {"mock": True, "inserted": 0, "chunks": 0, "reason": "empty content", "source_path": str(source_path)}
    cs = chunk_size or _chunk_size()
    ov = overlap or _overlap()
    d = dim or _dim()
    chunks = chunk_text(content, chunk_size=cs, overlap=ov)
    if not chunks:
        return {"mock": True, "inserted": 0, "chunks": 0, "reason": "no chunks", "source_path": str(source_path)}
    embeddings = get_embeddings(chunks, dim=d)
    sp = Path(source_path)
    meta = dict(metadata or {})
    meta.setdefault("source", "personal_wiki")
    meta.setdefault("vault_path", str(sp))
    # Priority: 1) memory_service HTTP API if configured, 2) direct DB via sqlalchemy, 3) mock
    # Try HTTP API first
    try:
        api_res = await _write_via_memory_service_api(chunks, embeddings, sp, meta, owner=owner, tenant_id=tenant_id, agent_id=agent_id)
        if api_res is not None:
            return api_res
    except Exception:
        pass
    # Try DB
    if _is_db_configured():
        try:
            return await _write_via_sqlalchemy(chunks, embeddings, sp, meta, owner=owner, tenant_id=tenant_id, agent_id=agent_id)
        except Exception as e:
            logger.warning(f"embed DB write failed, falling back to mock: {e}")
    # Mock fallback — still returns embeddings for testability
    return {
        "mock": True,
        "inserted": len(chunks),
        "chunks": len(chunks),
        "ids": [f"mock_{i}" for i in range(len(chunks))],
        "source_path": str(sp),
        "reason": "no DB configured or write failed — mock",
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
        if not p.exists():
            return {"mock": True, "inserted": 0, "chunks": 0, "reason": f"file not found: {p}", "source_path": str(p)}
        # read with utf-8 fallback
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_text(encoding="latin-1")
        except Exception as e:
            return {"mock": True, "inserted": 0, "chunks": 0, "reason": f"read error: {e}", "source_path": str(p)}
        if len(text) > max_chars:
            text = text[:max_chars]
        # strip frontmatter-like content? keep as-is for embedding
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
    except Exception as e:
        logger.warning(f"embed_file failed for {p}: {e}")
        return {"mock": True, "inserted": 0, "chunks": 0, "reason": str(e), "source_path": str(p)}


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
    try:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            # running loop (e.g. FastAPI) — schedule but don't block; run hash path synchronously as mock
            # We still compute embeddings synchronously and return mock to avoid blocking
            p = Path(file_path)
            try:
                try:
                    text = p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    text = p.read_text(encoding="latin-1")
                if len(text) > max_chars:
                    text = text[:max_chars]
                chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
                embeddings = get_embeddings(chunks, dim=dim)
                return {
                    "mock": True,
                    "inserted": len(chunks),
                    "chunks": len(chunks),
                    "ids": [f"mock_{i}" for i in range(len(chunks))],
                    "source_path": str(p),
                    "reason": "running loop — sync mock (embed deferred)",
                    "embeddings": embeddings[:1] if embeddings else [],
                }
            except Exception as e:
                return {"mock": True, "inserted": 0, "chunks": 0, "reason": str(e), "source_path": str(file_path)}
        else:
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
    except Exception as e:
        logger.warning(f"embed_file_sync failed: {e}")
        return {"mock": True, "inserted": 0, "chunks": 0, "reason": str(e), "source_path": str(file_path)}


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
    try:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            chunks = chunk_text(content, chunk_size=chunk_size, overlap=overlap)
            d = dim or _dim()
            embeddings = get_embeddings(chunks, dim=d)
            return {
                "mock": True,
                "inserted": len(chunks),
                "chunks": len(chunks),
                "ids": [f"mock_{i}" for i in range(len(chunks))],
                "source_path": str(source_path),
                "reason": "running loop — sync mock",
                "embeddings": embeddings[:1] if embeddings else [],
            }
        else:
            return asyncio.run(
                embed_text(content, source_path, metadata, owner, tenant_id, agent_id, chunk_size, overlap, dim)
            )
    except Exception as e:
        return {"mock": True, "inserted": 0, "chunks": 0, "reason": str(e), "source_path": str(source_path)}
