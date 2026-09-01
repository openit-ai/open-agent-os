from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_index.models import KnowledgeIndexEntry
from knowledge_index.repository import KnowledgeIndexRepository


@pytest.mark.asyncio
async def test_delete_by_resource_is_tenant_scoped() -> None:
    from knowledge_index.orm import Base, KnowledgeIndexORM

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    repository = KnowledgeIndexRepository(maker)
    for tenant, index_id in (("t1", "i1"), ("t2", "i2")):
        await repository.upsert(KnowledgeIndexEntry(
            index_id=index_id, source_system="outline", source_resource_id="doc", tenant_id=tenant,
            chunk_id=index_id, chunk_text="text", indexed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        ))
    assert await repository.delete_by_resource("t1", "doc") == 1
    assert await repository.get("i1") is None
    assert await repository.get("i2") is not None
    await engine.dispose()
