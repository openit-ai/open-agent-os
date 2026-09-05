"""ACL-pre-filter retrieval — lexical + pgvector + deterministic fallback.

Contract:
 - tenant_id is MANDATORY (ValueError if missing) — no cross-tenant leakage
 - ACL pre-filter BEFORE retrieval: WHERE tenant_id AND (group_id IS NULL OR group_id IN allowed) AND (agent_id IS NULL OR agent_id IN allowed)
 - lexical: LIKE %query% after pre-filter, routed as mode=lexical or hybrid
 - semantic: pgvector cosine (<->) when Postgres+pgvector available; else deterministic fallback ONLY when allowed and NOT in production
 - production (OAOS_ENV=production): deterministic fallback is blocked — raises RuntimeError (fail-closed, no mock permissiveness)
 - provenance always returned in RetrievalHit
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, or_, and_

from .repository import KnowledgeIndexRepository
from .orm import KnowledgeIndexORM
from .models import KnowledgeIndexEntry


def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        v = os.environ.get(k, "").strip().lower()
        if v in ("production", "prod"):
            return True
    return False


def _embedding_for_read(val: Any) -> list[float] | None:
    if val is None:
        return None
    if isinstance(val, list):
        return [float(x) for x in val]
    if isinstance(val, str):
        try:
            p = json.loads(val)
            if isinstance(p, list):
                return [float(x) for x in p]
        except Exception:
            return None
    return None


def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return -1.0
    return dot / (na * nb)


@dataclass
class RetrievalHit:
    index_id: str
    chunk_text: str
    chunk_id: str
    source_system: str
    source_resource_id: str
    source_uri: str | None
    tenant_id: str
    group_id: str | None
    agent_id: str | None
    content_hash: str | None
    acl_version: str | None
    classification: str | None
    retention_policy: str | None
    provenance: dict[str, Any] | None
    score: float | None = None
    indexed_at: Any = None
    source_updated_at: Any = None

    @classmethod
    def from_entry(cls, e: KnowledgeIndexEntry, score: float | None = None) -> "RetrievalHit":
        return cls(
            index_id=e.index_id,
            chunk_text=e.chunk_text,
            chunk_id=e.chunk_id,
            source_system=e.source_system,
            source_resource_id=e.source_resource_id,
            source_uri=e.source_uri,
            tenant_id=e.tenant_id,
            group_id=e.group_id,
            agent_id=e.agent_id,
            content_hash=e.content_hash,
            acl_version=e.acl_version,
            classification=e.classification,
            retention_policy=e.retention_policy,
            provenance=e.provenance,
            score=score,
            indexed_at=e.indexed_at,
            source_updated_at=e.source_updated_at,
        )


def _acl_clauses(tenant_id: str, allowed_group_ids: list[str], allowed_agent_ids: list[str]):
    clauses: list[Any] = [KnowledgeIndexORM.tenant_id == tenant_id]
    # group filter: (group_id IS NULL OR group_id IN allowed)
    if allowed_group_ids:
        clauses.append(or_(KnowledgeIndexORM.group_id.is_(None), KnowledgeIndexORM.group_id.in_(allowed_group_ids)))
    else:
        clauses.append(KnowledgeIndexORM.group_id.is_(None))
    # agent filter similarly
    if allowed_agent_ids:
        clauses.append(or_(KnowledgeIndexORM.agent_id.is_(None), KnowledgeIndexORM.agent_id.in_(allowed_agent_ids)))
    else:
        clauses.append(KnowledgeIndexORM.agent_id.is_(None))
    return clauses


class KnowledgeIndexRetriever:
    def __init__(self, repository: KnowledgeIndexRepository) -> None:
        self._repo = repository

    async def retrieve(
        self,
        *,
        query: str,
        tenant_id: str,
        allowed_group_ids: list[str] | None = None,
        allowed_agent_ids: list[str] | None = None,
        limit: int = 10,
        mode: str = "hybrid",  # lexical | semantic | hybrid
        query_embedding: list[float] | None = None,
        allow_deterministic_fallback: bool = False,
    ) -> list[RetrievalHit]:
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id is required — ACL pre-filter needs tenant scope")
        tenant_id = tenant_id.strip()
        if not query or not query.strip():
            return []
        allowed_group_ids = [g for g in (allowed_group_ids or []) if g and g.strip()]
        allowed_agent_ids = [a for a in (allowed_agent_ids or []) if a and a.strip()]
        limit = max(1, min(int(limit), 100))
        mode = (mode or "hybrid").lower().strip()
        query = query.strip()

        maker = self._repo._maker  # type: ignore[attr-defined]

        # Build base ACL pre-filter query
        base_clauses = _acl_clauses(tenant_id, allowed_group_ids, allowed_agent_ids)

        if mode == "semantic":
            return await self._semantic(maker, base_clauses, query, query_embedding, limit, allow_deterministic_fallback)
        if mode == "lexical":
            return await self._lexical(maker, base_clauses, query, limit)
        # hybrid: lexical + semantic rerank (or lexical fused)
        # For hybrid without embedding, fall back to lexical only
        if query_embedding is not None:
            lex = await self._lexical(maker, base_clauses, query, limit * 2)
            sem = await self._semantic(maker, base_clauses, query, query_embedding, limit * 2, allow_deterministic_fallback)
            # merge unique by index_id, preserve semantic order then lexical
            seen: set[str] = set()
            merged: list[RetrievalHit] = []
            for h in sem + lex:
                if h.index_id not in seen:
                    seen.add(h.index_id)
                    merged.append(h)
                if len(merged) >= limit:
                    break
            return merged[:limit]
        return await self._lexical(maker, base_clauses, query, limit)

    async def _lexical(self, maker, base_clauses, query: str, limit: int) -> list[RetrievalHit]:
        # Lexical: WHERE chunk_text LIKE %query% (case-insensitive)
        # Use ilike for Postgres, like for SQLite fallback
        pattern = f"%{query}%"
        async with maker() as session:
            # try ilike; fallback to like
            try:
                stmt = select(KnowledgeIndexORM).where(*base_clauses).where(KnowledgeIndexORM.chunk_text.ilike(pattern)).limit(limit)
                res = await session.execute(stmt)
            except Exception:
                stmt = select(KnowledgeIndexORM).where(*base_clauses).where(KnowledgeIndexORM.chunk_text.like(pattern)).limit(limit)
                res = await session.execute(stmt)
            hits: list[RetrievalHit] = []
            for row in res.scalars().all():
                emb = _embedding_for_read(row.embedding)
                entry = KnowledgeIndexEntry(
                    index_id=row.index_id,
                    source_system=row.source_system,
                    source_resource_id=row.source_resource_id,
                    source_uri=row.source_uri,
                    tenant_id=row.tenant_id,
                    group_id=row.group_id,
                    agent_id=row.agent_id,
                    chunk_id=row.chunk_id,
                    chunk_text=row.chunk_text,
                    embedding=emb,
                    content_hash=row.content_hash,
                    source_updated_at=row.source_updated_at,
                    indexed_at=row.indexed_at,
                    acl_version=row.acl_version,
                    classification=row.classification,
                    retention_policy=row.retention_policy,
                    provenance=row.provenance,
                )
                hits.append(RetrievalHit.from_entry(entry, score=None))
            return hits

    async def _semantic(self, maker, base_clauses, query: str, query_embedding: list[float] | None, limit: int, allow_fallback: bool) -> list[RetrievalHit]:
        if query_embedding is None:
            raise ValueError("query_embedding is required for semantic mode")
        # Detect if we can use pgvector: check if underlying engine url is postgres and pgvector available
        is_pg = False
        try:
            # inspect engine url
            bind = maker.kw.get("bind") if hasattr(maker, "kw") else None
            engine = getattr(maker, "bind", None) or bind
            if engine is not None:
                # AsyncEngine exposes the URL through sync_engine.
                url_str = str(getattr(engine, "url", "") or getattr(getattr(engine, "sync_engine", None), "url", ""))
                if "postgres" in url_str or "postgresql" in url_str:
                    is_pg = True
            # Environment is a fallback for custom sessionmaker wrappers.
            if not is_pg:
                db_url = os.environ.get("DATABASE_URL") or os.environ.get("OAOS_DATABASE_URL", "")
                if "postgres" in db_url:
                    is_pg = True
            # A configured repository with the pgvector ORM type is also a
            # positive signal when the async wrapper hides its bind URL.
            if not is_pg:
                try:
                    is_pg = "vector" in str(KnowledgeIndexORM.__table__.c.embedding.type).lower()
                except Exception:
                    pass
        except Exception:
            pass

        # Try pgvector path if postgres
        if is_pg:
            try:
                from pgvector.sqlalchemy import Vector  # type: ignore
            except Exception as exc:
                raise RuntimeError("pgvector SQLAlchemy integration is unavailable") from exc
            if Vector is not None:
                # pgvector cosine distance: embedding <=> :vec
                async with maker() as session:
                    # Use raw ORDER BY embedding <=> query_embedding if column type is VECTOR
                    # Fallback to python scoring if column not vector
                    # We attempt op via .op("<=>") if available
                    try:
                        # SQLAlchemy binds the query vector using the ORM
                        # column's VECTOR dimension/type. The live bge-m3
                        # contract is VECTOR(1024), established by migration
                        # 019; no TEXT <=> implicit cast is allowed.
                        from sqlalchemy import bindparam
                        from pgvector.sqlalchemy import Vector as _Vector
                        query_vector = bindparam("query_vector", value=query_embedding, type_=_Vector(len(query_embedding)))
                        distance = KnowledgeIndexORM.embedding.op("<=>")(query_vector)
                        stmt = select(KnowledgeIndexORM).where(*base_clauses).order_by(distance).limit(limit)
                        res = await session.execute(stmt)
                        hits: list[RetrievalHit] = []
                        for row in res.scalars().all():
                            emb = _embedding_for_read(row.embedding)
                            score = _cosine_sim(query_embedding, emb) if emb else None
                            entry = KnowledgeIndexEntry(
                                index_id=row.index_id,
                                source_system=row.source_system,
                                source_resource_id=row.source_resource_id,
                                source_uri=row.source_uri,
                                tenant_id=row.tenant_id,
                                group_id=row.group_id,
                                agent_id=row.agent_id,
                                chunk_id=row.chunk_id,
                                chunk_text=row.chunk_text,
                                embedding=emb,
                                content_hash=row.content_hash,
                                source_updated_at=row.source_updated_at,
                                indexed_at=row.indexed_at,
                                acl_version=row.acl_version,
                                classification=row.classification,
                                retention_policy=row.retention_policy,
                                provenance=row.provenance,
                            )
                            hits.append(RetrievalHit.from_entry(entry, score=score))
                        return hits
                    except Exception:
                        # pg query failed, fall through to deterministic fallback handling
                        pass

        # Non-postgres or pgvector not available -> deterministic fallback ONLY for tests
        if _is_production():
            raise RuntimeError("semantic search requires Postgres+pgvector in production — deterministic fallback is not allowed (OAOS_ENV=production)")
        if not allow_fallback:
            raise RuntimeError("semantic search requires Postgres+pgvector or allow_deterministic_fallback=True (test only)")
        # Deterministic fallback: fetch ACL-filtered candidates then score in Python
        async with maker() as session:
            stmt = select(KnowledgeIndexORM).where(*base_clauses).limit(200)
            res = await session.execute(stmt)
            candidates: list[tuple[float, KnowledgeIndexORM]] = []
            for row in res.scalars().all():
                emb = _embedding_for_read(row.embedding)
                score = _cosine_sim(query_embedding, emb) if emb else -1.0
                # slight lexical tie-breaker deterministic
                if query.lower() in (row.chunk_text or "").lower():
                    score += 0.001
                candidates.append((score, row))
            candidates.sort(key=lambda x: x[0], reverse=True)
            hits: list[RetrievalHit] = []
            for score, row in candidates[:limit]:
                emb = _embedding_for_read(row.embedding)
                entry = KnowledgeIndexEntry(
                    index_id=row.index_id,
                    source_system=row.source_system,
                    source_resource_id=row.source_resource_id,
                    source_uri=row.source_uri,
                    tenant_id=row.tenant_id,
                    group_id=row.group_id,
                    agent_id=row.agent_id,
                    chunk_id=row.chunk_id,
                    chunk_text=row.chunk_text,
                    embedding=emb,
                    content_hash=row.content_hash,
                    source_updated_at=row.source_updated_at,
                    indexed_at=row.indexed_at,
                    acl_version=row.acl_version,
                    classification=row.classification,
                    retention_policy=row.retention_policy,
                    provenance=row.provenance,
                )
                hits.append(RetrievalHit.from_entry(entry, score=score))
            return hits
