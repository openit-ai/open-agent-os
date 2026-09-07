"""Async repository for Knowledge Index — upsert / get / bulk / list."""
from __future__ import annotations

import json
import datetime as dt
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import KnowledgeIndexEntry
from .orm import KnowledgeIndexORM


def _coerce_embedding_for_write(emb: Any) -> Any:
    if emb is None:
        return None
    # if list[float], store JSON string for Text fallback; pgvector can ingest list via cast
    # We detect column type at runtime: if ORM column is Text, store json string
    # For now, store JSON string always for cross-compat — pgvector via psycopg can also handle string? Better keep list if possible.
    # Heuristic: if env DATABASE_URL contains postgres and pgvector installed, keep list.
    try:
        from pgvector.sqlalchemy import Vector  # type: ignore

        # if pgvector available, return list directly (SQLAlchemy will adapt)
        return emb
    except Exception:
        # fallback: serialize to JSON string for Text column
        if isinstance(emb, list):
            return json.dumps(emb)
        return emb


def _coerce_embedding_for_read(val: Any) -> list[float] | None:
    if val is None:
        return None
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return [float(x) for x in parsed]
        except Exception:
            return None
    return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _orm_to_entry(row: KnowledgeIndexORM) -> KnowledgeIndexEntry:
    return KnowledgeIndexEntry(
        index_id=row.index_id,
        source_system=row.source_system,
        source_resource_id=row.source_resource_id,
        source_uri=row.source_uri,
        tenant_id=row.tenant_id,
        group_id=row.group_id,
        agent_id=row.agent_id,
        chunk_id=row.chunk_id,
        chunk_text=row.chunk_text,
        embedding=_coerce_embedding_for_read(row.embedding),
        content_hash=row.content_hash,
        source_updated_at=row.source_updated_at,
        indexed_at=row.indexed_at,
        acl_version=row.acl_version,
        classification=row.classification,
        retention_policy=row.retention_policy,
        provenance=row.provenance,
    )


class KnowledgeIndexRepository:
    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._maker = session_maker

    async def upsert(self, entry: KnowledgeIndexEntry) -> KnowledgeIndexEntry:
        if not entry.tenant_id or not entry.tenant_id.strip():
            raise ValueError("tenant_id is required — cross-tenant isolation violation")
        if not entry.index_id or not entry.index_id.strip():
            raise ValueError("index_id required")
        now = entry.indexed_at or _utcnow()
        data = entry.model_dump()
        data["indexed_at"] = now
        # coerce embedding for DB
        data["embedding"] = _coerce_embedding_for_write(entry.embedding)
        async with self._maker() as session:
            existing = await session.get(KnowledgeIndexORM, entry.index_id)
            if existing is None:
                orm = KnowledgeIndexORM(**data)
                session.add(orm)
            else:
                for k, v in data.items():
                    setattr(existing, k, v)
            await session.commit()
            # re-read
            row = await session.get(KnowledgeIndexORM, entry.index_id)
            assert row is not None
            return _orm_to_entry(row)

    async def bulk_upsert(self, entries: list[KnowledgeIndexEntry]) -> list[KnowledgeIndexEntry]:
        out: list[KnowledgeIndexEntry] = []
        for e in entries:
            out.append(await self.upsert(e))
        return out

    async def get(self, index_id: str) -> KnowledgeIndexEntry | None:
        async with self._maker() as session:
            row = await session.get(KnowledgeIndexORM, index_id)
            if row is None:
                return None
            return _orm_to_entry(row)

    async def delete(self, index_id: str) -> bool:
        async with self._maker() as session:
            row = await session.get(KnowledgeIndexORM, index_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def delete_by_resource(self, tenant_id: str, source_resource_id: str) -> int:
        """Delete all derived chunks for one tenant-scoped source resource."""
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id required")
        if not source_resource_id or not source_resource_id.strip():
            raise ValueError("source_resource_id required")
        async with self._maker() as session:
            result = await session.execute(
                delete(KnowledgeIndexORM).where(
                    KnowledgeIndexORM.tenant_id == tenant_id.strip(),
                    KnowledgeIndexORM.source_resource_id == source_resource_id.strip(),
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def list_by_tenant(self, tenant_id: str, limit: int = 100) -> list[KnowledgeIndexEntry]:
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id required")
        async with self._maker() as session:
            res = await session.execute(
                select(KnowledgeIndexORM).where(KnowledgeIndexORM.tenant_id == tenant_id.strip()).limit(limit)
            )
            return [_orm_to_entry(r) for r in res.scalars().all()]
