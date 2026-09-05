from __future__ import annotations

from datetime import datetime, timezone
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.asyncio
async def test_acl_change_removes_old_public_rows() -> None:
    from knowledge_index.models import KnowledgeIndexEntry, SourceDocument
    from knowledge_index.repository import KnowledgeIndexRepository
    from knowledge_index.service import sync_outline_to_index
    from knowledge_index.store import InMemoryCheckpointStore
    from knowledge_index.connectors.base import FetchResult, SourceAdapter
    from knowledge_index.embedding import FakeEmbeddingProvider

    class Adapter(SourceAdapter):
        source_system = "outline"
        def __init__(self, doc): self.doc = doc
        def fetch(self, checkpoint=None): return FetchResult(documents=[self.doc])

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from knowledge_index.orm import KnowledgeIndexORM
    async with engine.begin() as c:
        await c.run_sync(lambda sc: KnowledgeIndexORM.__table__.create(sc, checkfirst=True))
    repo = KnowledgeIndexRepository(async_sessionmaker(engine, expire_on_commit=False))
    rid = "outline/team/acl-change"
    now = datetime.now(timezone.utc)
    await repo.upsert(KnowledgeIndexEntry(
        index_id="old-public", source_system="outline", source_resource_id=rid,
        source_uri="/doc/acl-change", tenant_id="tenant-a", group_id=None,
        agent_id=None, chunk_id="c1", chunk_text="secret policy", embedding=[0.0] * 8,
        content_hash="old", source_updated_at=now, indexed_at=now,
        acl_version="v1", classification="INTERNAL", provenance={"old": True},
    ))
    doc = SourceDocument(
        resource_id=rid, source_system="outline", title="ACL change",
        content="secret policy", source_updated_at=now.isoformat(),
        acl_version="v2", acl={"users": ["agent:assistant:alice"]}, source_uri="/doc/acl-change",
        tenant_id="tenant-a",
    )
    res = await sync_outline_to_index(
        tenant_id="tenant-a", repository=repo, embedding_provider=FakeEmbeddingProvider(dim=8),
        outline_adapter=Adapter(doc), checkpoint_store=InMemoryCheckpointStore(),
        resolve_outline_acl=False,
    )
    assert res.failed == 0
    rows = await repo.list_by_tenant("tenant-a", limit=20)
    assert rows and all(r.agent_id == "agent:assistant:alice" for r in rows)
    assert not any(r.group_id is None and r.agent_id is None for r in rows)
    await engine.dispose()
