"""Tests: Outline/Notion connector sync orchestration — incremental, idempotent, checkpointed."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "packages" / "knowledge-index"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

import pytest
from knowledge_index.chunking import ChunkConfig
from knowledge_index.embedding import FakeEmbeddingProvider
from knowledge_index.connectors.outline import OutlineSourceAdapter, make_outline_doc
from knowledge_index.connectors.notion import NotionSourceAdapter, make_notion_doc
from knowledge_index.connectors.base import InMemorySourceAdapter
from knowledge_index.models import SourceDocument
from knowledge_index.store import InMemoryChunkStore, InMemoryCheckpointStore
from knowledge_index.sync import SyncOrchestrator


def _doc_outline(doc_id="doc_001", content="hello", acl_version="v1", updated_at="2026-01-01T00:00:00+00:00"):
    return make_outline_doc(doc_id=doc_id, title=f"Doc {doc_id}", content=content, acl_version=acl_version, updated_at=updated_at)


def _doc_notion(page_id="page_001", content="notion hello", acl_version="v1", updated_at="2026-01-01T00:00:00+00:00"):
    return make_notion_doc(page_id=page_id, title=f"Page {page_id}", content=content, acl_version=acl_version, updated_at=updated_at)


class TestAdapterInterface:
    def test_outline_adapter_fetch(self):
        docs = [_doc_outline("doc_001", "a"), _doc_outline("doc_002", "b")]
        adapter = OutlineSourceAdapter(documents=docs)
        res = adapter.fetch(None)
        assert len(res.documents) == 2
        assert {d.resource_id for d in res.documents} == {"outline/team/doc_001", "outline/team/doc_002"}

    def test_notion_adapter_fetch(self):
        docs = [_doc_notion("p1", "x")]
        adapter = NotionSourceAdapter(documents=docs)
        res = adapter.fetch(None)
        assert res.documents[0].source_system == "notion"
        assert res.documents[0].acl_version == "v1"

    def test_acl_metadata_present(self):
        doc = make_outline_doc(doc_id="doc_acl", acl={"groups": ["admin"]}, acl_version="v2", content="secret")
        assert doc.acl == {"groups": ["admin"]}
        assert doc.acl_version == "v2"
        assert doc.source_updated_at is not None
        assert doc.content_hash is not None


class TestIncrementalSync:
    def test_initial_sync_upserts(self):
        docs = [_doc_outline("doc_001", "hello world " * 50), _doc_outline("doc_002", "second doc")]
        adapter = OutlineSourceAdapter(documents=docs)
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=32), chunk_store=store, checkpoint_store=cpoint, chunk_config=ChunkConfig(max_chars=800, overlap=100))
        res = orch.sync()
        assert res.fetched == 2
        assert res.upserted == 2
        assert res.skipped == 0
        assert res.deleted == 0
        assert store.count_chunks() >= 2
        assert cpoint.load("outline") is not None

    def test_skip_unchanged(self):
        docs = [_doc_outline("doc_001", "hello", updated_at="2026-01-01T00:00:00+00:00")]
        adapter = OutlineSourceAdapter(documents=docs)
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=16), chunk_store=store, checkpoint_store=cpoint)
        r1 = orch.sync()
        assert r1.upserted == 1
        r2 = orch.sync()
        assert r2.skipped == 1
        assert r2.upserted == 0
        assert r2.fetched == 1

    def test_upsert_on_content_change(self):
        doc_v1 = _doc_outline("doc_001", "hello v1", updated_at="2026-01-01T00:00:00+00:00")
        adapter = OutlineSourceAdapter(documents=[doc_v1])
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=16), chunk_store=store, checkpoint_store=cpoint)
        orch.sync()
        # modify content
        doc_v2 = _doc_outline("doc_001", "hello v2 changed", updated_at="2026-01-02T00:00:00+00:00")
        adapter.set_documents([doc_v2])
        # need to clear deleted tracking that set_documents adds? set_documents marks missing as deleted but we keep same id so no deletion
        res = orch.sync()
        assert res.upserted == 1
        assert res.skipped == 0
        # chunks updated
        sc = store.get("outline/team/doc_001")
        assert sc is not None
        assert sc.content_hash == doc_v2.content_hash

    def test_upsert_on_acl_version_change(self):
        doc_v1 = _doc_outline("doc_001", "same content", acl_version="v1", updated_at="2026-01-01T00:00:00+00:00")
        adapter = OutlineSourceAdapter(documents=[doc_v1])
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=16), chunk_store=store, checkpoint_store=cpoint)
        orch.sync()
        doc_v2 = _doc_outline("doc_001", "same content", acl_version="v2", updated_at="2026-01-01T00:00:00+00:00")
        adapter.set_documents([doc_v2])
        res = orch.sync()
        assert res.upserted == 1
        assert res.skipped == 0

    def test_upsert_on_source_updated_at_change(self):
        doc_v1 = _doc_outline("doc_001", "same", updated_at="2026-01-01T00:00:00+00:00")
        adapter = OutlineSourceAdapter(documents=[doc_v1])
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=16), chunk_store=store, checkpoint_store=cpoint)
        orch.sync()
        doc_v2 = _doc_outline("doc_001", "same", updated_at="2026-01-03T00:00:00+00:00")
        adapter.set_documents([doc_v2])
        res = orch.sync()
        assert res.upserted == 1

    def test_skip_requires_all_three_match(self):
        # Change only content_hash => must upsert even if other two same? But content_hash derived from content, so changing it implies content change
        doc = _doc_outline("doc_001", "hello", acl_version="v1", updated_at="2026-01-01T00:00:00+00:00")
        adapter = OutlineSourceAdapter(documents=[doc])
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=16), chunk_store=store, checkpoint_store=cpoint)
        orch.sync()
        # craft doc with same content but different hash artificially (should not happen normally but tests logic)
        doc2 = _doc_outline("doc_001", "hello", acl_version="v1", updated_at="2026-01-01T00:00:00+00:00")
        doc2.content_hash = "different_hash"
        adapter.set_documents([doc2])
        res = orch.sync()
        assert res.upserted == 1


class TestDeleteAndIdempotent:
    def test_delete_removed_resources(self):
        docs = [_doc_outline("doc_001", "a"), _doc_outline("doc_002", "b")]
        adapter = OutlineSourceAdapter(documents=docs)
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=16), chunk_store=store, checkpoint_store=cpoint)
        orch.sync()
        assert store.list_resource_ids() == {"outline/team/doc_001", "outline/team/doc_002"}
        # remove doc_002
        adapter.set_documents([_doc_outline("doc_001", "a")])
        res = orch.sync()
        assert res.deleted == 1
        assert store.list_resource_ids() == {"outline/team/doc_001"}
        assert "outline/team/doc_002" not in cpoint.load("outline").resource_states  # type: ignore

    def test_idempotent_runs(self):
        docs = [_doc_outline("doc_001", "idempotent content")]
        adapter = OutlineSourceAdapter(documents=docs)
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=16), chunk_store=store, checkpoint_store=cpoint)
        for _ in range(3):
            res = orch.sync()
        # last run should be all skipped
        assert res.skipped == 1
        assert res.upserted == 0
        assert res.deleted == 0
        # store not duplicated
        assert store.count_chunks() == len(store.get("outline/team/doc_001").chunks)  # type: ignore

    def test_delete_then_readd(self):
        docs = [_doc_outline("doc_001", "v1")]
        adapter = OutlineSourceAdapter(documents=docs)
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=16), chunk_store=store, checkpoint_store=cpoint)
        orch.sync()
        adapter.set_documents([])
        r2 = orch.sync()
        assert r2.deleted == 1
        adapter.set_documents([_doc_outline("doc_001", "v1")])
        # clear deleted tracking for re-add check: adapter still would have deleted empty then re-added, but set_documents tracks deleted correctly
        r3 = orch.sync()
        assert r3.upserted == 1
        assert store.list_resource_ids() == {"outline/team/doc_001"}


class TestRetriesAndCheckpoint:
    def test_bounded_retries_fetch(self):
        docs = [_doc_outline("doc_001", "retry test")]
        adapter = OutlineSourceAdapter(documents=docs, fail_times=2)
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=16), chunk_store=store, checkpoint_store=cpoint, max_retries=3, retry_backoff_s=0.001)
        res = orch.sync()
        assert res.fetched == 1
        assert res.upserted == 1
        assert adapter.fetch_calls == 3  # 2 failures + 1 success

    def test_fetch_exhausted_retries(self):
        adapter = OutlineSourceAdapter(documents=[_doc_outline("doc_001", "x")], fail_times=5)
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=16), chunk_store=store, checkpoint_store=cpoint, max_retries=3, retry_backoff_s=0.001)
        res = orch.sync()
        assert res.failed == 1
        assert len(res.errors) == 1
        assert "fetch failed" in res.errors[0]

    def test_checkpoint_persisted_across_orchestrators(self):
        docs = [_doc_outline("doc_001", "checkpoint")]
        adapter = OutlineSourceAdapter(documents=docs)
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch1 = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=16), chunk_store=store, checkpoint_store=cpoint)
        orch1.sync()
        # new orchestrator sharing same checkpoint store should skip
        orch2 = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=16), chunk_store=store, checkpoint_store=cpoint)
        res = orch2.sync()
        assert res.skipped == 1

    def test_notion_sync(self):
        docs = [_doc_notion("p1", "notion content " * 20)]
        adapter = NotionSourceAdapter(documents=docs)
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=32), chunk_store=store, checkpoint_store=cpoint, chunk_config=ChunkConfig(max_chars=200, overlap=20))
        res = orch.sync()
        assert res.fetched == 1
        assert res.upserted == 1
        assert store.count_chunks() >= 1
        assert res.checkpoint is not None
        assert res.checkpoint.source_system == "notion"

    def test_chunk_ids_stable_across_sync(self):
        content = "stable " * 200
        docs = [_doc_outline("doc_001", content)]
        adapter = OutlineSourceAdapter(documents=docs)
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=16), chunk_store=store, checkpoint_store=cpoint, chunk_config=ChunkConfig(max_chars=100, overlap=10))
        orch.sync()
        ids_first = [c.chunk_id for c in store.get("outline/team/doc_001").chunks]  # type: ignore
        # second sync skipped, ids unchanged
        orch.sync()
        ids_second = [c.chunk_id for c in store.get("outline/team/doc_001").chunks]  # type: ignore
        assert ids_first == ids_second
