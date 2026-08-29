"""Knowledge Index — persistence / ACL pre-filter retrieval (v1.7.1 §0.4).

TDD RED: expects packages/knowledge-index to exist with:
 - fields: index_id, source_system, source_resource_id, source_uri, tenant_id,
   group_id/agent_id, chunk_id, chunk_text, embedding VECTOR(1536) nullable (sqlite fallback),
   content_hash, source_updated_at, indexed_at, acl_version, classification, retention_policy, provenance
 - tenant + ACL pre-filter BEFORE retrieval (no post-filter, no cross-tenant leakage)
 - lexical search + pgvector semantic when Postgres, deterministic fallback for tests only
 - provenance in results, no mock permissiveness in production (OAOS_ENV=production blocks fallback)
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_entry(**over):
    from knowledge_index.models import KnowledgeIndexEntry  # type: ignore

    base = dict(
        index_id=f"idx_{uuid.uuid4().hex[:8]}",
        source_system="outline",
        source_resource_id="doc_123",
        source_uri="https://outline.example/doc/123",
        tenant_id="tenant-a",
        group_id=None,
        agent_id=None,
        chunk_id="chunk_1",
        chunk_text="Open Agent OS enterprise policy for PTO",
        embedding=None,
        content_hash="abc123",
        source_updated_at=datetime.now(timezone.utc),
        indexed_at=datetime.now(timezone.utc),
        acl_version="v1",
        classification="INTERNAL",
        retention_policy="standard",
        provenance={"source": "outline", "collection_id": "col1"},
    )
    base.update(over)
    return KnowledgeIndexEntry(**base)


async def _sqlite_maker():
    # Create only knowledge_index table (avoid duplicate-index bug in approval_nonces that breaks full metadata create_all on sqlite)
    from knowledge_index.orm import KnowledgeIndexORM  # type: ignore

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: KnowledgeIndexORM.__table__.create(sc, checkfirst=True))
    return async_sessionmaker(engine, expire_on_commit=False), engine


# ---------------------------------------------------------------------------
# schema field presence
# ---------------------------------------------------------------------------

async def test_schema_has_required_fields():
    from knowledge_index.orm import KnowledgeIndexORM  # type: ignore

    cols = {c.name for c in KnowledgeIndexORM.__table__.columns}
    required = {
        "index_id",
        "source_system",
        "source_resource_id",
        "source_uri",
        "tenant_id",
        "group_id",
        "agent_id",
        "chunk_id",
        "chunk_text",
        "embedding",
        "content_hash",
        "source_updated_at",
        "indexed_at",
        "acl_version",
        "classification",
        "retention_policy",
        "provenance",
    }
    missing = required - cols
    assert not missing, f"missing columns: {missing}"
    # embedding must be nullable
    assert KnowledgeIndexORM.__table__.c.embedding.nullable is True


async def test_embedding_nullable_sqlite_fallback():
    maker, engine = await _sqlite_maker()
    from knowledge_index.repository import KnowledgeIndexRepository  # type: ignore

    repo = KnowledgeIndexRepository(maker)
    e = _make_entry(embedding=None)
    await repo.upsert(e)
    got = await repo.get(e.index_id)
    assert got is not None
    assert got.embedding is None
    await engine.dispose()


# ---------------------------------------------------------------------------
# tenant isolation — no cross-tenant leakage
# ---------------------------------------------------------------------------

async def test_no_cross_tenant_leakage():
    maker, engine = await _sqlite_maker()
    from knowledge_index.repository import KnowledgeIndexRepository  # type: ignore
    from knowledge_index.retrieval import KnowledgeIndexRetriever  # type: ignore

    repo = KnowledgeIndexRepository(maker)
    e1 = _make_entry(tenant_id="tenant-a", chunk_text="unique alpha bravo", index_id="idx_a1")
    e2 = _make_entry(tenant_id="tenant-b", chunk_text="unique alpha bravo", index_id="idx_b1")
    await repo.bulk_upsert([e1, e2])

    retr = KnowledgeIndexRetriever(repo)
    # tenant-a must not see tenant-b docs
    res = await retr.retrieve(query="alpha bravo", tenant_id="tenant-a", allowed_group_ids=[], allowed_agent_ids=[], limit=10)
    ids = {r.index_id for r in res}
    assert "idx_a1" in ids
    assert "idx_b1" not in ids

    res2 = await retr.retrieve(query="alpha bravo", tenant_id="tenant-b", allowed_group_ids=[], allowed_agent_ids=[], limit=10)
    ids2 = {r.index_id for r in res2}
    assert "idx_b1" in ids2
    assert "idx_a1" not in ids2
    await engine.dispose()


async def test_tenant_required_rejects_empty():
    maker, engine = await _sqlite_maker()
    from knowledge_index.repository import KnowledgeIndexRepository  # type: ignore
    from knowledge_index.retrieval import KnowledgeIndexRetriever  # type: ignore

    repo = KnowledgeIndexRepository(maker)
    retr = KnowledgeIndexRetriever(repo)
    with pytest.raises((ValueError, Exception)):
        await retr.retrieve(query="hello", tenant_id="", allowed_group_ids=[], allowed_agent_ids=[])
    await engine.dispose()


# ---------------------------------------------------------------------------
# ACL pre-filter before retrieval
# ---------------------------------------------------------------------------

async def test_acl_pre_filter_group():
    maker, engine = await _sqlite_maker()
    from knowledge_index.repository import KnowledgeIndexRepository  # type: ignore
    from knowledge_index.retrieval import KnowledgeIndexRetriever  # type: ignore

    repo = KnowledgeIndexRepository(maker)
    e_public = _make_entry(tenant_id="tenant-a", index_id="idx_pub", group_id=None, agent_id=None, chunk_text="public enterprise handbook alpha")
    e_g1 = _make_entry(tenant_id="tenant-a", index_id="idx_g1", group_id="group-1", agent_id=None, chunk_text="group1 secret alpha")
    e_g2 = _make_entry(tenant_id="tenant-a", index_id="idx_g2", group_id="group-2", agent_id=None, chunk_text="group2 secret alpha")
    await repo.bulk_upsert([e_public, e_g1, e_g2])

    retr = KnowledgeIndexRetriever(repo)
    # user in group-1 should see public + g1, not g2
    res = await retr.retrieve(query="alpha", tenant_id="tenant-a", allowed_group_ids=["group-1"], allowed_agent_ids=[], limit=10)
    ids = {r.index_id for r in res}
    assert "idx_g1" in ids
    assert "idx_g2" not in ids
    # public always visible? spec says ACL pre-filter — public (null group/agent) is visible to all within tenant
    assert "idx_pub" in ids
    await engine.dispose()


async def test_acl_pre_filter_agent():
    maker, engine = await _sqlite_maker()
    from knowledge_index.repository import KnowledgeIndexRepository  # type: ignore
    from knowledge_index.retrieval import KnowledgeIndexRetriever  # type: ignore

    repo = KnowledgeIndexRepository(maker)
    e = _make_entry(tenant_id="tenant-a", index_id="idx_agent", group_id=None, agent_id="agent:assistant:alice", chunk_text="alice private alpha")
    await repo.upsert(e)
    retr = KnowledgeIndexRetriever(repo)
    res_ok = await retr.retrieve(query="alpha", tenant_id="tenant-a", allowed_group_ids=[], allowed_agent_ids=["agent:assistant:alice"], limit=10)
    assert any(r.index_id == "idx_agent" for r in res_ok)
    res_no = await retr.retrieve(query="alpha", tenant_id="tenant-a", allowed_group_ids=[], allowed_agent_ids=["agent:assistant:bob"], limit=10)
    assert all(r.index_id != "idx_agent" for r in res_no)
    await engine.dispose()


# ---------------------------------------------------------------------------
# lexical search
# ---------------------------------------------------------------------------

async def test_lexical_search():
    maker, engine = await _sqlite_maker()
    from knowledge_index.repository import KnowledgeIndexRepository  # type: ignore
    from knowledge_index.retrieval import KnowledgeIndexRetriever  # type: ignore

    repo = KnowledgeIndexRepository(maker)
    await repo.bulk_upsert([
        _make_entry(index_id="idx_lex1", chunk_text="kubernetes deployment guide"),
        _make_entry(index_id="idx_lex2", chunk_text="postgres pgvector semantic search"),
    ])
    retr = KnowledgeIndexRetriever(repo)
    res = await retr.retrieve(query="kubernetes", tenant_id="tenant-a", allowed_group_ids=[], allowed_agent_ids=[], limit=10, mode="lexical")
    ids = [r.index_id for r in res]
    assert "idx_lex1" in ids
    assert "idx_lex2" not in ids
    await engine.dispose()


# ---------------------------------------------------------------------------
# deterministic fallback — test-only, blocked in production
# ---------------------------------------------------------------------------

async def test_semantic_deterministic_fallback_test_only():
    maker, engine = await _sqlite_maker()
    from knowledge_index.repository import KnowledgeIndexRepository  # type: ignore
    from knowledge_index.retrieval import KnowledgeIndexRetriever  # type: ignore

    repo = KnowledgeIndexRepository(maker)
    # embeddings present as plain list fallback on sqlite
    await repo.bulk_upsert([
        _make_entry(index_id="idx_s1", chunk_text="hello world", embedding=[0.1] * 4),
        _make_entry(index_id="idx_s2", chunk_text="goodbye world", embedding=[0.9] * 4),
    ])
    retr = KnowledgeIndexRetriever(repo)
    # deterministic fallback allowed in non-prod (tests)
    res1 = await retr.retrieve(query="hello", tenant_id="tenant-a", allowed_group_ids=[], allowed_agent_ids=[], limit=2, mode="semantic", query_embedding=[0.1] * 4, allow_deterministic_fallback=True)
    res2 = await retr.retrieve(query="hello", tenant_id="tenant-a", allowed_group_ids=[], allowed_agent_ids=[], limit=2, mode="semantic", query_embedding=[0.1] * 4, allow_deterministic_fallback=True)
    assert [r.index_id for r in res1] == [r.index_id for r in res2], "deterministic fallback must be stable"
    await engine.dispose()


async def test_semantic_no_mock_permissiveness_in_production(monkeypatch):
    maker, engine = await _sqlite_maker()
    from knowledge_index.repository import KnowledgeIndexRepository  # type: ignore
    from knowledge_index.retrieval import KnowledgeIndexRetriever  # type: ignore

    repo = KnowledgeIndexRepository(maker)
    await repo.upsert(_make_entry(index_id="idx_prod", chunk_text="hello", embedding=[0.1] * 4))
    retr = KnowledgeIndexRetriever(repo)
    monkeypatch.setenv("OAOS_ENV", "production")
    # In production, semantic without pgvector must NOT silently use deterministic fallback
    # It should raise or return empty with explicit error, not leak via mock
    with pytest.raises(Exception):
        await retr.retrieve(query="hello", tenant_id="tenant-a", allowed_group_ids=[], allowed_agent_ids=[], limit=2, mode="semantic", query_embedding=[0.1] * 4, allow_deterministic_fallback=True)
    monkeypatch.delenv("OAOS_ENV", raising=False)
    await engine.dispose()


# ---------------------------------------------------------------------------
# provenance in results
# ---------------------------------------------------------------------------

async def test_provenance_in_results():
    maker, engine = await _sqlite_maker()
    from knowledge_index.repository import KnowledgeIndexRepository  # type: ignore
    from knowledge_index.retrieval import KnowledgeIndexRetriever  # type: ignore

    repo = KnowledgeIndexRepository(maker)
    e = _make_entry(index_id="idx_prov", provenance={"collection_id": "col_99", "updated_by": "alice"})
    await repo.upsert(e)
    retr = KnowledgeIndexRetriever(repo)
    res = await retr.retrieve(query="policy", tenant_id="tenant-a", allowed_group_ids=[], allowed_agent_ids=[], limit=10)
    assert len(res) >= 1
    hit = next(r for r in res if r.index_id == "idx_prov")
    assert hit.provenance is not None
    assert hit.source_uri == "https://outline.example/doc/123"
    assert hit.source_system == "outline"
    assert hit.content_hash == "abc123"
    await engine.dispose()
