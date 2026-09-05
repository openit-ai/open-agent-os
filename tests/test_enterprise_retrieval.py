"""Focused enterprise retrieval tests — owner/tenant/group ACL + failure behavior.

Uses in-memory SQLite + injected KnowledgeIndexRepository (no prod DB,
no external systems). Covers the control-plane enterprise path only;
connector/sync files are untouched.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio


def _make_entry(**over):
    from knowledge_index.models import KnowledgeIndexEntry
    base = dict(
        index_id=f"idx_{uuid.uuid4().hex[:8]}",
        source_system="outline",
        source_resource_id="doc_1",
        source_uri="https://outline.example/doc/1",
        tenant_id="tenant-a",
        group_id=None,
        agent_id=None,
        chunk_id="chunk_1",
        chunk_text="enterprise synctest policy handbook",
        embedding=None,
        content_hash="h1",
        source_updated_at=datetime.now(timezone.utc),
        indexed_at=datetime.now(timezone.utc),
        acl_version="v1",
        classification="INTERNAL",
        retention_policy="standard",
        provenance={"source": "outline", "collection_id": "col1"},
    )
    base.update(over)
    return KnowledgeIndexEntry(**base)


async def _repo_with(entries):
    from knowledge_index.orm import KnowledgeIndexORM
    from knowledge_index.repository import KnowledgeIndexRepository
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: KnowledgeIndexORM.__table__.create(sc, checkfirst=True))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    repo = KnowledgeIndexRepository(maker)
    await repo.bulk_upsert(entries)
    return repo, engine


async def test_group_acl_fail_closed_by_default():
    from control_plane.context_retrieval import retrieve_enterprise_context
    entries = [
        _make_entry(index_id="idx_pub", group_id=None, agent_id=None),
        _make_entry(index_id="idx_mine", group_id=None, agent_id="agent:assistant:kim"),
        _make_entry(index_id="idx_other", group_id=None, agent_id="agent:assistant:lee"),
        _make_entry(index_id="idx_g1", group_id="group-1", agent_id=None),
        _make_entry(index_id="idx_g2", group_id="group-2", agent_id=None),
    ]
    repo, engine = await _repo_with(entries)
    try:
        hits = await retrieve_enterprise_context(
            "tenant-a", "agent:assistant:kim", "synctest", repository=repo,
        )
        ids = {h["index_id"] for h in hits}
        assert "idx_pub" in ids
        assert "idx_mine" in ids
        assert "idx_other" not in ids  # other owner's agent scope hidden
        assert "idx_g1" not in ids  # group scope hidden without membership
        assert "idx_g2" not in ids
    finally:
        await engine.dispose()


async def test_group_acl_member_sees_only_own_group():
    from control_plane.context_retrieval import retrieve_enterprise_context
    entries = [
        _make_entry(index_id="idx_pub", group_id=None, agent_id=None),
        _make_entry(index_id="idx_g1", group_id="group-1", agent_id=None),
        _make_entry(index_id="idx_g2", group_id="group-2", agent_id=None),
    ]
    repo, engine = await _repo_with(entries)
    try:
        hits = await retrieve_enterprise_context(
            "tenant-a", "agent:assistant:kim", "synctest",
            allowed_group_ids=["group-1"], repository=repo,
        )
        ids = {h["index_id"] for h in hits}
        assert "idx_pub" in ids
        assert "idx_g1" in ids
        assert "idx_g2" not in ids
    finally:
        await engine.dispose()


async def test_tenant_isolation_no_cross_leak():
    from control_plane.context_retrieval import retrieve_enterprise_context
    entries = [
        _make_entry(index_id="idx_a", tenant_id="tenant-a"),
        _make_entry(index_id="idx_b", tenant_id="tenant-b"),
    ]
    repo, engine = await _repo_with(entries)
    try:
        hits_a = await retrieve_enterprise_context(
            "tenant-a", "agent:assistant:kim", "synctest", repository=repo,
        )
        ids_a = {h["index_id"] for h in hits_a}
        assert "idx_a" in ids_a
        assert "idx_b" not in ids_a
        hits_b = await retrieve_enterprise_context(
            "tenant-b", "agent:assistant:kim", "synctest", repository=repo,
        )
        ids_b = {h["index_id"] for h in hits_b}
        assert "idx_b" in ids_b
        assert "idx_a" not in ids_b
    finally:
        await engine.dispose()


async def test_missing_tenant_or_agent_raises():
    from control_plane.context_retrieval import retrieve_enterprise_context
    repo, engine = await _repo_with([_make_entry(index_id="idx_x")])
    try:
        with pytest.raises(ValueError, match="tenant_id"):
            await retrieve_enterprise_context("", "agent:assistant:kim", "synctest", repository=repo)
        with pytest.raises(ValueError, match="tenant_id"):
            await retrieve_enterprise_context("   ", "agent:assistant:kim", "synctest", repository=repo)
        with pytest.raises(ValueError, match="agent_id"):
            await retrieve_enterprise_context("tenant-a", "", "synctest", repository=repo)
    finally:
        await engine.dispose()


async def test_empty_query_returns_empty_without_db_error():
    from control_plane.context_retrieval import retrieve_enterprise_context
    hits = await retrieve_enterprise_context("tenant-a", "agent:assistant:kim", "   ", repository=None)
    assert hits == []


async def test_infra_failure_raises_not_silent_empty():
    from control_plane.context_retrieval import EnterpriseRetrievalError, retrieve_enterprise_context

    class _BrokenMaker:
        def __call__(self, *a, **k):
            raise RuntimeError("db down")

    class _BrokenRepo:
        _maker = _BrokenMaker()

    with pytest.raises(EnterpriseRetrievalError, match="unavailable"):
        await retrieve_enterprise_context(
            "tenant-a", "agent:assistant:kim", "synctest", repository=_BrokenRepo(),
        )


async def test_provenance_preserved():
    from control_plane.context_retrieval import retrieve_enterprise_context
    prov = {"source": "outline", "collection_id": "col_99", "updated_by": "alice"}
    entries = [_make_entry(
        index_id="idx_prov", provenance=prov,
        source_uri="https://outline.example/doc/99",
        source_resource_id="doc-99",
    )]
    repo, engine = await _repo_with(entries)
    try:
        hits = await retrieve_enterprise_context(
            "tenant-a", "agent:assistant:kim", "synctest", repository=repo,
        )
        hit = next(h for h in hits if h["index_id"] == "idx_prov")
        assert hit["provenance"] == prov
        assert hit["source_uri"] == "https://outline.example/doc/99"
        assert hit["source_resource_id"] == "doc-99"
        assert hit["tenant_id"] == "tenant-a"
    finally:
        await engine.dispose()
