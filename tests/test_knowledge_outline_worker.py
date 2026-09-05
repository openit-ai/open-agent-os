"""Focused tests for the minimal safe Outline->Index production sync fix.

Covers (no live network, no production DB, sqlite only):
1. Root/packages http_outline mirrors both honor bounded max_pages.
2. sync_outline_to_index preserves metadata (ACL groups, title, source_uri)
   via StoredChunks + fresh per-run snapshot — Http adapter has no _docs.
3. Empty-content docs clean previously persisted rows (delete_by_resource).
4. Explicit source deletions propagate to the persistent repository.
5. KnowledgeSyncService.sync_to_persistent delegates; missing pieces -> ValueError.
6. Worker guards: production refuses fake embeddings; missing tenant exits 2;
   implicit fake refused without explicit opt-in.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_index.connectors.base import SourceAdapter


async def _sqlite_maker():
    from knowledge_index.orm import KnowledgeIndexORM

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: KnowledgeIndexORM.__table__.create(sc, checkfirst=True))
    return async_sessionmaker(engine, expire_on_commit=False), engine


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeTransport:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}), "json": json, "timeout": timeout})
        if not self.responses:
            raise RuntimeError(f"no queued response for {url}")
        nxt = self.responses.pop(0)
        return nxt if isinstance(nxt, FakeResp) else FakeResp(200, nxt)


def _raw_doc(doc_id="doc_001", collection="team", title="T", text="hello world sync test",
             updated_at="2026-01-01T00:00:00Z", acl=None, url=""):
    d = {"id": doc_id, "collectionId": collection, "title": title, "text": text, "updatedAt": updated_at}
    if acl is not None:
        d["acl"] = acl
    if url:
        d["url"] = url
    return d


def _pages(n, per_page=2):
    docs = [
        _raw_doc(doc_id=f"doc_{i:03d}", title=f"Doc {i}", text=f"content body {i} " * 10)
        for i in range(n)
    ]
    return [docs[i:i + per_page] for i in range(0, n, per_page)]


class TestMaxPagesBound:
    pytestmark = pytest.mark.asyncio

    @pytest.mark.parametrize("mod", [
        "knowledge_index.connectors.http_outline",
    ])
    async def test_root_adapter_respects_max_pages(self, mod):
        import importlib

        m = importlib.import_module(mod)
        chunks = _pages(6, per_page=2)  # 3 pages available
        responses = [
            {"data": c, "pagination": {"offset": i * 2, "total": 6}}
            for i, c in enumerate(chunks)
        ]
        adapter = m.HttpOutlineSourceAdapter(
            api_url="https://o.example.com", api_token="tok",
            http_client=FakeTransport(responses=list(responses)),
            page_limit=2, max_pages=1, retry_backoff_s=0.001,
        )
        res = adapter.fetch(None)
        assert adapter.last_fetch_pages == 1
        assert len(res.documents) == 2

    async def test_package_mirror_respects_max_pages(self):
        import sys
        from pathlib import Path

        pkg = Path(__file__).resolve().parents[1] / "packages" / "knowledge-index"
        assert (pkg / "knowledge_index" / "connectors" / "http_outline.py").exists()
        if str(pkg) not in sys.path:
            sys.path.insert(0, str(pkg))
        # Mirror shares package-relative imports, so verify parity textually instead.
        src = (pkg / "knowledge_index" / "connectors" / "http_outline.py").read_text()
        assert "max_pages: int | None = None" in src
        assert "self.max_pages = max_pages if max_pages is None else max(1, int(max_pages))" in src
        assert "max_pages = self.max_pages or 500" in src
        root = (Path(__file__).resolve().parents[1] / "knowledge_index" / "connectors" / "http_outline.py").read_text()
        # Mirror must not drift from root on the pagination contract.
        for snippet in ("max_pages: int | None = None", "max_pages = self.max_pages or 500"):
            assert snippet in root


class TestMetadataPreservation:
    pytestmark = pytest.mark.asyncio

    async def test_acl_title_uri_preserved_without_docs_mirror(self):
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        from knowledge_index.embedding import FakeEmbeddingProvider
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.service import sync_outline_to_index

        maker, engine = await _sqlite_maker()
        repo = KnowledgeIndexRepository(maker)
        raw = _raw_doc(doc_id="doc_001", title="Secret Plan", text="hello world sync test " * 10,
                       acl={"groups": ["eng"]}, url="https://o.example.com/doc/doc_001")
        adapter = HttpOutlineSourceAdapter(
            api_url="https://o.example.com", api_token="tok",
            http_client=FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}]),
            retry_backoff_s=0.001,
        )
        assert not hasattr(adapter, "_docs")  # Http adapter has no private mirror
        result = await sync_outline_to_index(
            tenant_id="tenant-a", repository=repo,
            embedding_provider=FakeEmbeddingProvider(dim=8), outline_adapter=adapter,
        )
        assert result.persisted >= 1
        rows = await repo.list_by_tenant("tenant-a", limit=10)
        assert rows, "expected persisted rows"
        assert all(r.group_id == "eng" for r in rows), "restricted doc must not persist as public"
        assert all((r.provenance or {}).get("acl_groups") == ["eng"] for r in rows)
        assert all((r.provenance or {}).get("title") == "Secret Plan" for r in rows)
        assert all(r.source_uri == "https://o.example.com/doc/doc_001" for r in rows)
        await engine.dispose()

    async def test_empty_content_cleans_prior_rows(self):
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        from knowledge_index.embedding import FakeEmbeddingProvider
        from knowledge_index.models import KnowledgeIndexEntry
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.service import sync_outline_to_index

        maker, engine = await _sqlite_maker()
        repo = KnowledgeIndexRepository(maker)
        rid = "outline/team/doc_empty"
        now = datetime.now(timezone.utc)
        await repo.upsert(KnowledgeIndexEntry(
            index_id="stale_idx", source_system="outline", source_resource_id=rid,
            source_uri="https://o.example.com/doc/doc_empty", tenant_id="tenant-a",
            group_id=None, agent_id=None, chunk_id="c1", chunk_text="stale text",
            embedding=[0.1] * 8, content_hash="old", source_updated_at=now,
            indexed_at=now, acl_version="v1", classification="INTERNAL",
            retention_policy=None, provenance={"source": "outline"},
        ))
        raw = _raw_doc(doc_id="doc_empty", title="Now Empty", text="   ")
        adapter = HttpOutlineSourceAdapter(
            api_url="https://o.example.com", api_token="tok",
            http_client=FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}]),
            retry_backoff_s=0.001,
        )
        result = await sync_outline_to_index(
            tenant_id="tenant-a", repository=repo,
            embedding_provider=FakeEmbeddingProvider(dim=8), outline_adapter=adapter,
        )
        assert result.failed == 0
        assert await repo.list_by_tenant("tenant-a", limit=10) == []
        await engine.dispose()

    async def test_explicit_deletion_propagates(self):
        from knowledge_index.connectors.base import InMemorySourceAdapter
        from knowledge_index.embedding import FakeEmbeddingProvider
        from knowledge_index.models import SourceDocument
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.service import sync_outline_to_index

        maker, engine = await _sqlite_maker()
        repo = KnowledgeIndexRepository(maker)
        doc = SourceDocument(
            resource_id="outline/team/doc_gone", source_system="outline",
            title="Gone", content="to be deleted content " * 10,
            source_updated_at="2026-01-01T00:00:00+00:00", tenant_id="tenant-a",
        )
        adapter = InMemorySourceAdapter(documents=[doc])
        first = await sync_outline_to_index(
            tenant_id="tenant-a", repository=repo,
            embedding_provider=FakeEmbeddingProvider(dim=8), outline_adapter=adapter,
        )
        assert first.persisted >= 1
        adapter.delete_document("outline/team/doc_gone")
        second = await sync_outline_to_index(
            tenant_id="tenant-a", repository=repo,
            embedding_provider=FakeEmbeddingProvider(dim=8), outline_adapter=adapter,
        )
        assert "outline/team/doc_gone" in (second.deleted_resource_ids or [])
        assert await repo.list_by_tenant("tenant-a", limit=10) == []
        await engine.dispose()


class TestSyncServiceDelegation:
    pytestmark = pytest.mark.asyncio

    async def test_delegates_with_context(self):
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        from knowledge_index.embedding import FakeEmbeddingProvider
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.service import KnowledgeSyncService

        maker, engine = await _sqlite_maker()
        repo = KnowledgeIndexRepository(maker)
        raw = _raw_doc(doc_id="doc_del", text="delegated persistent sync")
        adapter = HttpOutlineSourceAdapter(
            api_url="https://o.example.com", api_token="tok",
            http_client=FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}]),
            retry_backoff_s=0.001,
        )
        result = await KnowledgeSyncService().sync_to_persistent(
            tenant_id="t-del", repository=repo,
            embedding_provider=FakeEmbeddingProvider(dim=8), outline_adapter=adapter,
        )
        assert result.persisted >= 1
        await engine.dispose()

    async def test_missing_pieces_raise_value_error(self):
        from knowledge_index.service import KnowledgeSyncService

        with pytest.raises(ValueError, match="persistent sync requires"):
            await KnowledgeSyncService().sync_to_persistent()
        with pytest.raises(ValueError, match="missing"):
            await KnowledgeSyncService().sync_to_persistent(tenant_id="t1")


class TestWorkerGuards:
    def test_missing_tenant_exits_2(self, capsys):
        from knowledge_index.worker_outline_sync import main

        env = {k: v for k, v in os.environ.items() if k not in ("OAOS_TENANT_ID", "OAOS_CP_TENANT_ID", "TENANT_ID")}
        # keep test env marker so nothing treats this as production
        env["OAOS_ENV"] = "development"
        old = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            rc = main(["--database-url", "sqlite+aiosqlite:///:memory:"])
        finally:
            os.environ.clear()
            os.environ.update(old)
        assert rc == 2

    async def test_implicit_fake_refused_without_flag(self):
        from knowledge_index.worker_outline_sync import do_sync_batches

        old_env = dict(os.environ)
        os.environ["OAOS_ENV"] = "development"
        for k in ("OAOS_EMBED_API_URL", "OAOS_EMBEDDING_API_URL", "OLLAMA_API_URL", "OLLAMA_HOST"):
            os.environ.pop(k, None)
        try:
            with pytest.raises(RuntimeError, match="allow-fake-embed"):
                await do_sync_batches(
                    tenant_id="t1", collection_id=None,
                    db_url="sqlite+aiosqlite:///:memory:",
                    page_limit=5, max_pages=1, max_batches=1,
                    embed_dim=8, allow_fake_embed=False,
                )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_production_refuses_fake_even_with_flag(self, monkeypatch):
        import asyncio

        from knowledge_index.worker_outline_sync import do_sync_batches

        monkeypatch.setenv("OAOS_ENV", "production")
        for k in ("OAOS_EMBED_API_URL", "OAOS_EMBEDDING_API_URL", "OLLAMA_API_URL", "OLLAMA_HOST"):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(RuntimeError, match="production"):
            asyncio.run(do_sync_batches(
                tenant_id="t1", collection_id=None,
                db_url="sqlite+aiosqlite:///:memory:",
                page_limit=5, max_pages=1, max_batches=1,
                embed_dim=8, allow_fake_embed=True,
            ))

    def test_parser_bounds(self):
        from knowledge_index.worker_outline_sync import build_parser

        args = build_parser().parse_args(["--page-limit", "500", "--max-pages", "3"])
        assert args.page_limit == 500  # clamped at runtime, parser keeps raw
        assert args.max_pages == 3

    def test_loop_prune_flags(self):
        from knowledge_index.worker_outline_sync import build_parser

        args = build_parser().parse_args([])
        assert args.loop is False
        assert args.interval_s == 300.0
        assert args.prune_absent is False
        assert args.persist_batch_size == 200
        args = build_parser().parse_args(["--loop", "--interval-s", "5", "--prune-absent", "--persist-batch-size", "1"])
        assert args.loop is True
        assert args.interval_s == 5
        assert args.prune_absent is True
        assert args.persist_batch_size == 1


class _StubAdapter(SourceAdapter):
    """Minimal fixed-window source adapter (no deletion tracking)."""

    source_system = "outline"

    def __init__(self, docs, *, has_more=False, next_cursor=None):
        self._docs = list(docs)
        self._has_more = bool(has_more)
        self._next_cursor = next_cursor

    def fetch(self, checkpoint=None):
        from knowledge_index.connectors.base import FetchResult

        return FetchResult(
            documents=list(self._docs),
            deleted_resource_ids=[],
            next_cursor=self._next_cursor,
            has_more=self._has_more,
        )


def _stub_doc(rid="outline/team/doc_a", text="prune target content " * 10):
    from knowledge_index.models import SourceDocument

    return SourceDocument(
        resource_id=rid, source_system="outline", title=rid,
        content=text, source_updated_at="2026-01-01T00:00:00+00:00",
        tenant_id="tenant-a",
    )


class TestPaginationHasMore:
    pytestmark = pytest.mark.asyncio

    async def test_truncated_window_reports_has_more_and_resumes(self):
        import importlib

        from knowledge_index.models import SyncCheckpoint

        m = importlib.import_module("knowledge_index.connectors.http_outline")
        chunks = _pages(6, per_page=2)
        responses = [
            {"data": c, "pagination": {"offset": i * 2, "total": 6}}
            for i, c in enumerate(chunks)
        ]
        adapter = m.HttpOutlineSourceAdapter(
            api_url="https://o.example.com", api_token="tok",
            http_client=FakeTransport(responses=list(responses)),
            page_limit=2, max_pages=1, retry_backoff_s=0.001,
        )
        first = adapter.fetch(None)
        assert len(first.documents) == 2
        assert first.has_more is True  # window truncated, more on server
        assert first.next_cursor == "2"

        adapter2 = m.HttpOutlineSourceAdapter(
            api_url="https://o.example.com", api_token="tok",
            http_client=FakeTransport(responses=list(responses[1:])),
            page_limit=2, max_pages=10, retry_backoff_s=0.001,
        )
        rest = adapter2.fetch(SyncCheckpoint(source_system="outline", cursor="2"))
        assert len(rest.documents) == 4
        assert rest.has_more is False  # complete remainder
        # package mirror keeps the truncation contract
        from pathlib import Path

        pkg = Path(__file__).resolve().parents[1] / "packages" / "knowledge-index"
        src = (pkg / "knowledge_index" / "connectors" / "http_outline.py").read_text()
        assert "has_more=truncated" in src


class TestCompleteSnapshotPrune:
    pytestmark = pytest.mark.asyncio

    async def test_absent_pruned_only_when_explicit_and_complete(self):
        from knowledge_index.embedding import FakeEmbeddingProvider
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.service import sync_outline_to_index
        from knowledge_index.store import InMemoryCheckpointStore

        maker, engine = await _sqlite_maker()
        repo = KnowledgeIndexRepository(maker)
        store = InMemoryCheckpointStore()
        provider = FakeEmbeddingProvider(dim=8)
        doc_a = _stub_doc("outline/team/doc_a")
        doc_b = _stub_doc("outline/team/doc_b")

        first = await sync_outline_to_index(
            tenant_id="tenant-a", repository=repo, embedding_provider=provider,
            outline_adapter=_StubAdapter([doc_a, doc_b]), checkpoint_store=store,
        )
        assert first.failed == 0 and first.persisted >= 2
        assert {r.source_resource_id for r in await repo.list_by_tenant("tenant-a", limit=20)} == {
            "outline/team/doc_a", "outline/team/doc_b"}

        # doc_b disappears from source with NO explicit deletion IDs.
        second = await sync_outline_to_index(
            tenant_id="tenant-a", repository=repo, embedding_provider=provider,
            outline_adapter=_StubAdapter([doc_a]), checkpoint_store=store,
        )
        assert second.failed == 0
        remaining = {r.source_resource_id for r in await repo.list_by_tenant("tenant-a", limit=20)}
        assert remaining == {"outline/team/doc_a", "outline/team/doc_b"}  # default: no prune

        third = await sync_outline_to_index(
            tenant_id="tenant-a", repository=repo, embedding_provider=provider,
            outline_adapter=_StubAdapter([doc_a]), checkpoint_store=store,
            prune_absent_on_complete_snapshot=True, persist_batch_size=1,
        )
        assert third.failed == 0
        assert "outline/team/doc_b" in (third.deleted_resource_ids or [])
        remaining = {r.source_resource_id for r in await repo.list_by_tenant("tenant-a", limit=20)}
        assert remaining == {"outline/team/doc_a"}
        cp = store.load("outline")
        assert cp is not None and "outline/team/doc_b" not in (cp.resource_states or {})
        await engine.dispose()

    async def test_truncated_window_never_prunes(self):
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        from knowledge_index.embedding import FakeEmbeddingProvider
        from knowledge_index.models import KnowledgeIndexEntry, ResourceState, SyncCheckpoint
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.service import sync_outline_to_index
        from knowledge_index.store import InMemoryCheckpointStore

        maker, engine = await _sqlite_maker()
        repo = KnowledgeIndexRepository(maker)
        now = datetime.now(timezone.utc)
        stale = "outline/team/doc_stale"
        await repo.upsert(KnowledgeIndexEntry(
            index_id="stale_idx", source_system="outline", source_resource_id=stale,
            source_uri="https://o.example.com/doc/doc_stale", tenant_id="tenant-a",
            group_id=None, agent_id=None, chunk_id="c1", chunk_text="stale text",
            embedding=[0.1] * 8, content_hash="old", source_updated_at=now,
            indexed_at=now, acl_version="v1", classification="INTERNAL",
            retention_policy=None, provenance={"source": "outline"},
        ))
        store = InMemoryCheckpointStore()
        store.save(SyncCheckpoint(
            source_system="outline", cursor=None,
            resource_states={stale: ResourceState(content_hash="old", source_updated_at=now.isoformat(), acl_version="v1")},
        ))
        chunks = _pages(4, per_page=2)
        adapter = HttpOutlineSourceAdapter(
            api_url="https://o.example.com", api_token="tok",
            http_client=FakeTransport(responses=[
                {"data": chunks[0], "pagination": {"offset": 0, "total": 4}},
            ]),
            page_limit=2, max_pages=1, retry_backoff_s=0.001,
        )
        result = await sync_outline_to_index(
            tenant_id="tenant-a", repository=repo,
            embedding_provider=FakeEmbeddingProvider(dim=8), outline_adapter=adapter,
            checkpoint_store=store, prune_absent_on_complete_snapshot=True,
            resolve_outline_acl=False,
        )
        assert result.failed == 0
        remaining = {r.source_resource_id for r in await repo.list_by_tenant("tenant-a", limit=20)}
        assert stale in remaining  # truncated window must not prune
        await engine.dispose()

    async def test_resumed_empty_tail_never_prunes(self):
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        from knowledge_index.embedding import FakeEmbeddingProvider
        from knowledge_index.models import KnowledgeIndexEntry, ResourceState
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.service import sync_outline_to_index
        from knowledge_index.store import InMemoryCheckpointStore

        maker, engine = await _sqlite_maker()
        repo = KnowledgeIndexRepository(maker)
        store = InMemoryCheckpointStore()
        raw = _raw_doc(doc_id="doc_001", text="hello world sync test " * 10)
        full = HttpOutlineSourceAdapter(
            api_url="https://o.example.com", api_token="tok",
            http_client=FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}]),
            retry_backoff_s=0.001,
        )
        first = await sync_outline_to_index(
            tenant_id="tenant-a", repository=repo,
            embedding_provider=FakeEmbeddingProvider(dim=8), outline_adapter=full,
            checkpoint_store=store, resolve_outline_acl=False,
        )
        assert first.failed == 0 and first.persisted >= 1
        assert first.has_more is False
        # stale row appears after the cursor already reached the end
        now = datetime.now(timezone.utc)
        stale = "outline/team/doc_stale"
        await repo.upsert(KnowledgeIndexEntry(
            index_id="stale_idx", source_system="outline", source_resource_id=stale,
            source_uri="https://o.example.com/doc/doc_stale", tenant_id="tenant-a",
            group_id=None, agent_id=None, chunk_id="c1", chunk_text="stale text",
            embedding=[0.1] * 8, content_hash="old", source_updated_at=now,
            indexed_at=now, acl_version="v1", classification="INTERNAL",
            retention_policy=None, provenance={"source": "outline"},
        ))
        cp = store.load("outline")
        assert cp is not None and cp.cursor is None  # completed scan resets to offset 0
        cp.resource_states[stale] = ResourceState(
            content_hash="old", source_updated_at=now.isoformat(), acl_version="v1")
        store.save(cp)
        tail = HttpOutlineSourceAdapter(
            api_url="https://o.example.com", api_token="tok",
            http_client=FakeTransport(responses=[{"data": [], "pagination": {"offset": 1, "total": 1}}]),
            retry_backoff_s=0.001,
        )
        second = await sync_outline_to_index(
            tenant_id="tenant-a", repository=repo,
            embedding_provider=FakeEmbeddingProvider(dim=8), outline_adapter=tail,
            checkpoint_store=store, prune_absent_on_complete_snapshot=True,
            resolve_outline_acl=False,
        )
        assert second.failed == 0 and second.fetched == 0
        remaining = {r.source_resource_id for r in await repo.list_by_tenant("tenant-a", limit=20)}
        assert stale in remaining  # resumed empty tail must not prune
        await engine.dispose()
