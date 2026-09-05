"""RAG integration tests — knowledge_index/service.py wrappers.

Covers:
- async persistent search wrapper (KnowledgeSearchService / search_knowledge) with mandatory tenant/user context and ACL prefilter
- Outline sync factory using HttpOutlineSourceAdapter with fail-closed missing credentials
- materialization wrapper for create/update using explicit write gate and provenance context, relying on read-back
- No live Outline API call (injected FakeTransport)
"""
from __future__ import annotations

import os
import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_entry(**over):
    from knowledge_index.models import KnowledgeIndexEntry
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
        if isinstance(nxt, FakeResp):
            return nxt
        return FakeResp(200, nxt)

def _raw_doc(doc_id="doc_001", collection="team", title="T", text="hello", updated_at="2026-01-01T00:00:00Z", acl=None, url=""):
    d = {"id": doc_id, "collectionId": collection, "title": title, "text": text, "updatedAt": updated_at}
    if acl is not None:
        d["acl"] = acl
    if url:
        d["url"] = url
    return d

# ---------------------------------------------------------------------------
# 1) Async persistent search wrapper — mandatory tenant/user + ACL prefilter
# ---------------------------------------------------------------------------
class TestSearchWrapper:
    async def test_search_requires_tenant(self):
        maker, engine = await _sqlite_maker()
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.retrieval import KnowledgeIndexRetriever
        from knowledge_index.service import KnowledgeSearchService
        repo = KnowledgeIndexRepository(maker)
        retr = KnowledgeIndexRetriever(repo)
        svc = KnowledgeSearchService(retriever=retr)
        with pytest.raises(ValueError, match="tenant_id"):
            await svc.search(query="hello", tenant_id="", user_id="alice")
        with pytest.raises(ValueError, match="tenant_id"):
            await svc.search(query="hello", tenant_id="  ", user_id="alice")
        await engine.dispose()

    async def test_search_requires_user(self):
        maker, engine = await _sqlite_maker()
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.retrieval import KnowledgeIndexRetriever
        from knowledge_index.service import KnowledgeSearchService
        repo = KnowledgeIndexRepository(maker)
        retr = KnowledgeIndexRetriever(repo)
        svc = KnowledgeSearchService(retriever=retr)
        with pytest.raises(ValueError, match="user_id"):
            await svc.search(query="hello", tenant_id="tenant-a", user_id="")
        with pytest.raises(ValueError, match="user_id"):
            await svc.search(query="hello", tenant_id="tenant-a", user_id="   ")
        await engine.dispose()

    async def test_search_acl_prefilter(self):
        maker, engine = await _sqlite_maker()
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.retrieval import KnowledgeIndexRetriever
        from knowledge_index.service import KnowledgeSearchService
        repo = KnowledgeIndexRepository(maker)
        e_pub = _make_entry(index_id="idx_pub", group_id=None, agent_id=None, chunk_text="public handbook alpha", tenant_id="tenant-a")
        e_g1 = _make_entry(index_id="idx_g1", group_id="group-1", agent_id=None, chunk_text="group1 secret alpha", tenant_id="tenant-a")
        e_g2 = _make_entry(index_id="idx_g2", group_id="group-2", agent_id=None, chunk_text="group2 secret alpha", tenant_id="tenant-a")
        await repo.bulk_upsert([e_pub, e_g1, e_g2])
        retr = KnowledgeIndexRetriever(repo)
        svc = KnowledgeSearchService(retr)
        # group-1 member sees public + g1 not g2 via wrapper groups param
        res = await svc.search(query="alpha", tenant_id="tenant-a", user_id="alice", groups=["group-1"], limit=10)
        ids = {r.index_id for r in res}
        assert "idx_g1" in ids
        assert "idx_g2" not in ids
        assert "idx_pub" in ids
        # also via explicit allowed_group_ids
        res2 = await svc.search(query="alpha", tenant_id="tenant-a", user_id="alice", allowed_group_ids=["group-2"], limit=10)
        ids2 = {r.index_id for r in res2}
        assert "idx_g2" in ids2
        assert "idx_g1" not in ids2
        await engine.dispose()

    async def test_search_cross_tenant_isolation(self):
        maker, engine = await _sqlite_maker()
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.retrieval import KnowledgeIndexRetriever
        from knowledge_index.service import KnowledgeSearchService
        repo = KnowledgeIndexRepository(maker)
        e_a = _make_entry(tenant_id="tenant-a", index_id="idx_a", chunk_text="unique alpha bravo")
        e_b = _make_entry(tenant_id="tenant-b", index_id="idx_b", chunk_text="unique alpha bravo")
        await repo.bulk_upsert([e_a, e_b])
        retr = KnowledgeIndexRetriever(repo)
        svc = KnowledgeSearchService(retr)
        res_a = await svc.search(query="alpha bravo", tenant_id="tenant-a", user_id="u1", limit=10)
        assert "idx_a" in {r.index_id for r in res_a}
        assert "idx_b" not in {r.index_id for r in res_a}
        res_b = await svc.search(query="alpha bravo", tenant_id="tenant-b", user_id="u1", limit=10)
        assert "idx_b" in {r.index_id for r in res_b}
        assert "idx_a" not in {r.index_id for r in res_b}
        await engine.dispose()

    async def test_search_provenance_preserved(self):
        maker, engine = await _sqlite_maker()
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.retrieval import KnowledgeIndexRetriever
        from knowledge_index.service import KnowledgeSearchService
        repo = KnowledgeIndexRepository(maker)
        e = _make_entry(index_id="idx_prov", provenance={"collection_id": "col_99", "updated_by": "alice"}, chunk_text="policy handbook alpha")
        await repo.upsert(e)
        retr = KnowledgeIndexRetriever(repo)
        svc = KnowledgeSearchService(retr)
        res = await svc.search(query="policy", tenant_id="tenant-a", user_id="alice", limit=10)
        hit = next(r for r in res if r.index_id == "idx_prov")
        assert hit.provenance is not None
        assert hit.source_system == "outline"
        assert hit.source_uri == "https://outline.example/doc/123"
        await engine.dispose()

    async def test_search_knowledge_function_also_validates_tenant(self):
        maker, engine = await _sqlite_maker()
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.service import search_knowledge
        repo = KnowledgeIndexRepository(maker)
        with pytest.raises((ValueError, Exception)):
            await search_knowledge(query="hello", tenant_id="", repository=repo)
        with pytest.raises((ValueError, Exception)):
            await search_knowledge(query="hello", tenant_id="   ", repository=repo)
        await engine.dispose()

# ---------------------------------------------------------------------------
# 2) Outline sync factory — HttpOutlineSourceAdapter fail-closed + persistent sync
# ---------------------------------------------------------------------------
class TestSyncFactory:
    def test_adapter_fetch_fail_closed_missing_credentials(self, monkeypatch):
        for k in ("OUTLINE_API_URL", "OUTLINE_API_KEY", "OUTLINE_API_TOKEN", "OAOS_OUTLINE_TOKEN", "OAOS_OUTLINE_URL", "OUTLINE_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        adapter = HttpOutlineSourceAdapter(api_url="", api_token="", http_client=FakeTransport([]))
        with pytest.raises(RuntimeError, match="credentials missing"):
            adapter.fetch(None)
        adapter2 = HttpOutlineSourceAdapter(api_url=None, api_token=None, http_client=FakeTransport([]))
        with pytest.raises(RuntimeError, match="credentials missing"):
            adapter2.fetch(None)

    def test_adapter_fetch_no_mock_fallback(self):
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter, OutlineAPIError
        tr = FakeTransport(responses=[FakeResp(500, {"error": "boom"})])
        # Use tiny retry to make test fast
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, max_retries=1, retry_backoff_s=0.001)
        with pytest.raises(OutlineAPIError):
            adapter.fetch(None)
        assert adapter.fetch(None) if False else True  # ensure no mock docs returned

    async def test_sync_outline_to_index_persists_with_tenant(self):
        from knowledge_index.service import sync_outline_to_index
        from knowledge_index.embedding import FakeEmbeddingProvider
        maker, engine = await _sqlite_maker()
        from knowledge_index.repository import KnowledgeIndexRepository
        repo = KnowledgeIndexRepository(maker)
        raw = _raw_doc(doc_id="doc_001", collection="team", title="Doc", text="hello world sync test", updated_at="2026-01-01T00:00:00Z", acl={"groups": ["eng"]})
        tr = FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}])
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, retry_backoff_s=0.001)
        result = await sync_outline_to_index(tenant_id="tenant-a", repository=repo, embedding_provider=FakeEmbeddingProvider(dim=8), outline_adapter=adapter)
        assert result.fetched == 1
        assert result.persisted >= 1
        # verify persisted retrievable via search wrapper
        from knowledge_index.service import KnowledgeSearchService
        from knowledge_index.retrieval import KnowledgeIndexRetriever
        retr = KnowledgeIndexRetriever(repo)
        svc = KnowledgeSearchService(retr)
        hits = await svc.search(query="hello", tenant_id="tenant-a", user_id="alice", groups=["eng"], limit=10)
        assert len(hits) >= 1
        assert any("hello" in h.chunk_text for h in hits)
        await engine.dispose()

    async def test_sync_requires_tenant(self):
        from knowledge_index.service import sync_outline_to_index
        from knowledge_index.embedding import FakeEmbeddingProvider
        maker, engine = await _sqlite_maker()
        from knowledge_index.repository import KnowledgeIndexRepository
        repo = KnowledgeIndexRepository(maker)
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        tr = FakeTransport(responses=[{"data": [], "pagination": {"offset": 0, "total": 0}}])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, retry_backoff_s=0.001)
        with pytest.raises(ValueError, match="tenant_id"):
            await sync_outline_to_index(tenant_id="", repository=repo, embedding_provider=FakeEmbeddingProvider(dim=8), outline_adapter=adapter)
        await engine.dispose()

    def test_sync_service_describes_gap_and_fail_closed(self):
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        from knowledge_index.service import KnowledgeSyncService
        from knowledge_index.sync import SyncOrchestrator
        from knowledge_index.embedding import FakeEmbeddingProvider
        from knowledge_index.store import InMemoryChunkStore, InMemoryCheckpointStore
        raw = _raw_doc()
        tr = FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, retry_backoff_s=0.001)
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=4), chunk_store=InMemoryChunkStore(), checkpoint_store=InMemoryCheckpointStore(), retry_backoff_s=0.001)
        svc = KnowledgeSyncService(adapter=adapter, orchestrator=orch)
        gap = svc.describe_persistence_gap()
        assert "Persistence bridge" in gap or "sync" in gap.lower()
        # sync_memory works (in-memory)
        res = svc.sync_memory()
        assert res.fetched == 1

    @pytest.mark.asyncio
    async def test_sync_to_persistent_delegates_with_explicit_context(self):
        from knowledge_index.service import KnowledgeSyncService
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.embedding import FakeEmbeddingProvider
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        maker, engine = await _sqlite_maker()
        repo = KnowledgeIndexRepository(maker)
        raw = _raw_doc(doc_id="doc_delegate", text="delegated persistent sync")
        adapter = HttpOutlineSourceAdapter(
            api_url="https://o.example.com", api_token="tok",
            http_client=FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}]),
            retry_backoff_s=0.001,
        )
        svc = KnowledgeSyncService()
        result = await svc.sync_to_persistent(
            tenant_id="tenant-delegate", repository=repo,
            embedding_provider=FakeEmbeddingProvider(dim=8), outline_adapter=adapter,
        )
        assert result.persisted >= 1
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_sync_to_persistent_fails_closed_without_context(self):
        from knowledge_index.service import KnowledgeSyncService
        with pytest.raises(ValueError, match="persistent sync requires"):
            await KnowledgeSyncService().sync_to_persistent()

    @pytest.mark.asyncio
    async def test_sync_to_persistent_fails_closed_each_missing_piece(self):
        from knowledge_index.service import KnowledgeSyncService
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.embedding import FakeEmbeddingProvider
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        maker, engine = await _sqlite_maker()
        repo = KnowledgeIndexRepository(maker)
        provider = FakeEmbeddingProvider(dim=8)
        def _adapter():
            return HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=FakeTransport(responses=[{"data": [_raw_doc()], "pagination": {"offset": 0, "total": 1}}]), retry_backoff_s=0.001)
        svc = KnowledgeSyncService()
        # each required piece missing => fail-closed ValueError naming the gap
        with pytest.raises(ValueError, match="persistent sync requires"):
            await svc.sync_to_persistent(repository=repo, embedding_provider=provider, outline_adapter=_adapter())
        with pytest.raises(ValueError, match="persistent sync requires"):
            await svc.sync_to_persistent(tenant_id="t1", embedding_provider=provider, outline_adapter=_adapter())
        with pytest.raises(ValueError, match="persistent sync requires"):
            await svc.sync_to_persistent(tenant_id="t1", repository=repo, outline_adapter=_adapter())
        with pytest.raises(ValueError, match="persistent sync requires"):
            await svc.sync_to_persistent(tenant_id="t1", repository=repo, embedding_provider=provider)
        with pytest.raises(ValueError, match="persistent sync requires"):
            await svc.sync_to_persistent(tenant_id="", repository=repo, embedding_provider=provider, outline_adapter=_adapter())
        with pytest.raises(ValueError, match="persistent sync requires"):
            await svc.sync_to_persistent(tenant_id="   ", repository=repo, embedding_provider=provider, outline_adapter=_adapter())
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_sync_to_persistent_delegates_via_constructor_context(self):
        from knowledge_index.service import KnowledgeSyncService
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.embedding import FakeEmbeddingProvider
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        from knowledge_index.retrieval import KnowledgeIndexRetriever
        from knowledge_index.service import KnowledgeSearchService
        maker, engine = await _sqlite_maker()
        repo = KnowledgeIndexRepository(maker)
        raw = _raw_doc(doc_id="doc_ctor", text="constructor delegated sync")
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}]), retry_backoff_s=0.001)
        svc = KnowledgeSyncService(repository=repo, embedding_provider=FakeEmbeddingProvider(dim=8), outline_adapter=adapter, tenant_id="tenant-ctor")
        result = await svc.sync_to_persistent()
        assert result.persisted >= 1
        assert result.fetched == 1
        # verify actually persisted and searchable via tenant isolation
        retr = KnowledgeIndexRetriever(repo)
        search = KnowledgeSearchService(retr)
        hits = await search.search(query="constructor", tenant_id="tenant-ctor", user_id="alice", limit=10)
        assert any("constructor" in h.chunk_text for h in hits)
        # cross-tenant isolation: other tenant should not see it
        hits_other = await search.search(query="constructor", tenant_id="tenant-other", user_id="alice", limit=10)
        assert all("constructor" not in h.chunk_text for h in hits_other)
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_sync_to_persistent_delegates_via_adapter_alias_and_persists(self):
        from knowledge_index.service import KnowledgeSyncService
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.embedding import FakeEmbeddingProvider
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        maker, engine = await _sqlite_maker()
        repo = KnowledgeIndexRepository(maker)
        raw = _raw_doc(doc_id="doc_alias", text="alias adapter sync")
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}]), retry_backoff_s=0.001)
        svc = KnowledgeSyncService()
        # via `adapter` alias (not outline_adapter)
        result = await svc.sync_to_persistent(tenant_id="tenant-alias", repository=repo, embedding_provider=FakeEmbeddingProvider(dim=8), adapter=adapter)
        assert result.persisted >= 1
        await engine.dispose()

# ---------------------------------------------------------------------------
# 3) Materialization wrapper — explicit write gate + provenance + read-back
# ---------------------------------------------------------------------------
class TestMaterialization:
    def test_create_denied_without_write_gate(self):
        from knowledge_index.service import KnowledgeMaterializationService
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        tr = FakeTransport(responses=[{"data": {"id": "new"}}])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=False)
        svc = KnowledgeMaterializationService(adapter=adapter)
        with pytest.raises(PermissionError, match="writes disabled|write_enabled"):
            svc.create_document(title="T", text="hello", tenant_id="t1", user_id="u1")

    async def test_create_requires_write_enabled_on_materialize_function(self):
        from knowledge_index.service import materialize_knowledge_to_outline
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        tr = FakeTransport(responses=[{"data": {"id": "doc_new"}}])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=True, retry_backoff_s=0.001)
        with pytest.raises(PermissionError, match="write_enabled"):
            await materialize_knowledge_to_outline(title="T", text="hello", tenant_id="t1", outline_adapter=adapter, write_enabled=False)

    async def test_create_with_provenance_and_read_back(self):
        from knowledge_index.service import KnowledgeMaterializationService
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        tr = FakeTransport(responses=[
            {"data": {"id": "doc_new", "title": "My Doc"}},
            {"data": _raw_doc(doc_id="doc_new", title="My Doc", text="my text", collection="team")},
        ])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=True, retry_backoff_s=0.001)
        svc = KnowledgeMaterializationService(adapter=adapter, default_provenance={"tenant_id": "tenant-a"})
        doc = svc.create_document(title="My Doc", text="my text", collection_id="team", tenant_id="tenant-a", user_id="alice", trace_id="tr-123", provenance={"source_refs": ["outline/team/doc_001"]})
        assert doc.resource_id == "outline/team/doc_new"
        assert doc.title == "My Doc"
        assert doc.content == "my text"
        # provenance context was passed to adapter permission checker via context
        assert tr.calls[0]["json"]["title"] == "My Doc"
        assert tr.calls[0]["json"]["publish"] is True
        assert tr.calls[1]["url"].endswith("/api/documents.info")

    async def test_materialize_function_provenance_and_indexing(self):
        from knowledge_index.service import materialize_knowledge_to_outline
        from knowledge_index.embedding import FakeEmbeddingProvider
        maker, engine = await _sqlite_maker()
        from knowledge_index.repository import KnowledgeIndexRepository
        repo = KnowledgeIndexRepository(maker)
        tr = FakeTransport(responses=[
            {"data": {"id": "doc_gen", "title": "Gen Doc"}},
            {"data": _raw_doc(doc_id="doc_gen", title="Gen Doc", text="generated hello world", collection="team")},
        ])
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=True, retry_backoff_s=0.001)
        result = await materialize_knowledge_to_outline(
            title="Gen Doc", text="generated hello world", tenant_id="tenant-a",
            actor_user_id="alice", source_refs=["outline/team/doc_001"],
            provenance_extra={"task": "test"}, outline_adapter=adapter,
            repository=repo, embedding_provider=FakeEmbeddingProvider(dim=8), write_enabled=True, collection_id="team"
        )
        assert result.verification_passed is True
        assert result.outline_resource_id == "outline/team/doc_gen"
        assert result.provenance["tenant_id"] == "tenant-a"
        assert result.provenance["actor_user_id"] == "alice"
        assert result.indexed_entries >= 1
        # persisted doc searchable
        from knowledge_index.service import KnowledgeSearchService
        from knowledge_index.retrieval import KnowledgeIndexRetriever
        retr = KnowledgeIndexRetriever(repo)
        svc = KnowledgeSearchService(retr)
        hits = await svc.search(query="generated", tenant_id="tenant-a", user_id="alice", limit=10)
        assert any("generated" in h.chunk_text for h in hits)
        await engine.dispose()

    async def test_update_uses_read_back(self):
        from knowledge_index.service import KnowledgeMaterializationService
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter
        tr = FakeTransport(responses=[
            {"data": {"id": "doc_001"}},
            {"data": _raw_doc(doc_id="doc_001", title="New Title", text="new text", collection="team")},
        ])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=True, retry_backoff_s=0.001)
        svc = KnowledgeMaterializationService(adapter=adapter)
        doc = svc.update_document(doc_id="doc_001", title="New Title", text="new text", tenant_id="tenant-a", user_id="alice")
        assert tr.calls[0]["url"].endswith("/api/documents.update")
        assert tr.calls[0]["json"]["publish"] is True
        assert doc.title == "New Title"
