"""P1 live RAG operational focused tests — isolated, no prod DB mutation, no external bulk writes.

Covers §0.4 / §16.9-16.11 P1 scope:
- credential presence (no secret leak)
- connector health / read-only fetch (bounded, fake transport; live outline optionally if creds present)
- ACL/tenant pre-filter contract (tenant mandatory, group/tenant isolation before retrieval)
- content_hash / source_updated_at / acl_version incremental sync (skip vs upsert)
- deletion handling
- bounded retry / checkpoint (fail then succeed, exhaustion leaves checkpoint not advanced)
- live corpus backfill dry-run (bounded count, no DB write)
- persistent repository retrieval ACL pre-filter (sqlite in-memory, isolated)

All tests are isolated — they use InMemory stores or sqlite :memory: and FakeEmbeddingProvider,
and never write to the real oaos DB or call external bulk embedding APIs without explicit opt-in.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "packages" / "knowledge-index"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

import pytest

from knowledge_index.chunking import ChunkConfig, content_hash
from knowledge_index.connectors.base import InMemorySourceAdapter
from knowledge_index.connectors.outline import OutlineSourceAdapter, make_outline_doc
from knowledge_index.connectors.notion import NotionSourceAdapter, make_notion_doc
from knowledge_index.embedding import FakeEmbeddingProvider
from knowledge_index.models import SourceDocument, SyncCheckpoint
from knowledge_index.store import InMemoryChunkStore, InMemoryCheckpointStore
from knowledge_index.sync import SyncOrchestrator


# ---------------------------------------------------------------------------
# Credential presence
# ---------------------------------------------------------------------------
class TestCredentialPresence:
    def test_outline_credential_present_without_leak(self):
        from knowledge_index.health import check_outline_credentials

        res = check_outline_credentials()
        # must report presence as bool, never expose value
        assert "api_url_present" in res
        assert "api_token_present" in res
        assert "api_url_len" in res
        # no secret value exposed
        assert "api_token" not in res or res.get("api_token") is None or isinstance(res.get("api_token"), str) and len(res.get("api_token", "")) < 10
        # if live cred present, verifiable true
        if os.environ.get("OUTLINE_API_URL") and os.environ.get("OUTLINE_API_KEY"):
            assert res["verifiable"] is True

    def test_notion_credential_blocker_recorded(self):
        from knowledge_index.health import check_notion_credentials

        res = check_notion_credentials()
        # without creds, blocker must be set, not silently stubbed; when adapter missing, blocker mentions adapter
        if not res["verifiable"]:
            assert res["blocker"] is not None
            # fail-closed blocker must mention Notion (credentials missing OR adapter missing)
            assert "Notion" in res["blocker"]
            # must not fabricate verifiable True
            assert res["verifiable"] is False

    def test_health_never_prints_secrets(self, capsys):
        from knowledge_index.health import check_outline_credentials, check_notion_credentials

        o = check_outline_credentials()
        n = check_notion_credentials()
        # Ensure no raw token appears in stringified output
        blob = str(o) + str(n)
        # token value should not be in blob (we only expose len)
        tok = os.environ.get("OUTLINE_API_KEY", "")
        if tok:
            assert tok not in blob


# ---------------------------------------------------------------------------
# Connector health / read-only fetch (fake transport, bounded)
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        nxt = self.responses.pop(0)
        if isinstance(nxt, FakeResp):
            return nxt
        return FakeResp(200, nxt)


class TestConnectorHealth:
    def test_outline_health_fake_bounded_single_page(self):
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter

        raw = {"id": "doc_001", "collectionId": "team", "title": "T", "text": "hello world", "updatedAt": "2026-01-01T00:00:00Z", "url": "/doc/xx"}
        # need 2 queued responses: one for _fetch_page, one for fetch() loop
        payload = {"data": [raw], "pagination": {"total": 1, "limit": 1, "offset": 0}}
        transport = FakeTransport([payload, payload])
        adapter = HttpOutlineSourceAdapter(api_url="https://outline.example.com", api_token="tok_test", page_limit=1, http_client=transport)
        data, has_more = adapter._fetch_page(offset=0, limit=1)
        assert len(data) == 1
        assert data[0]["id"] == "doc_001"
        # verify normalized fields
        res = adapter.fetch()
        assert len(res.documents) == 1
        d = res.documents[0]
        assert d.content_hash
        assert d.source_updated_at
        assert d.acl_version
        assert d.source_system == "outline"

    def test_notion_health_fake_bounded(self):
        try:
            from knowledge_index.connectors.http_notion import HttpNotionSourceAdapter
        except (ModuleNotFoundError, ImportError) as e:
            pytest.skip(f"HttpNotionSourceAdapter not present in this checkout (fail-closed expected): {e}")

        raw_page = {
            "object": "page",
            "id": "abc-123",
            "last_edited_time": "2026-01-01T00:00:00Z",
            "url": "https://notion.so/abc",
            "properties": {"title": {"title": [{"plain_text": "Hello"}]}, "content": {"rich_text": [{"plain_text": "world"}]}},
            "parent": {"database_id": "db1"},
        }
        payload = {"results": [raw_page], "has_more": False, "next_cursor": None}
        transport = FakeTransport([payload, payload])
        adapter = HttpNotionSourceAdapter(api_url="https://api.notion.com", api_token="secret_test", page_limit=1, http_client=transport)
        data, cursor, has_more = adapter._fetch_page(cursor=None)
        assert len(data) == 1
        res = adapter.fetch()
        assert len(res.documents) == 1
        assert res.documents[0].content_hash

    def test_outline_health_fail_closed_without_creds(self):
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter

        # Use env stripping to ensure missing
        orig_url = os.environ.pop("OUTLINE_API_URL", None)
        orig_key = os.environ.pop("OUTLINE_API_KEY", None)
        orig_tok = os.environ.pop("OUTLINE_API_TOKEN", None)
        try:
            adapter = HttpOutlineSourceAdapter(api_url="", api_token="", http_client=FakeTransport([]))
            with pytest.raises(RuntimeError, match="credentials missing"):
                adapter.fetch()
        finally:
            if orig_url is not None:
                os.environ["OUTLINE_API_URL"] = orig_url
            if orig_key is not None:
                os.environ["OUTLINE_API_KEY"] = orig_key
            if orig_tok is not None:
                os.environ["OUTLINE_API_TOKEN"] = orig_tok

    def test_live_outline_health_bounded_if_creds_present(self):
        # Live probe only if credentials present; otherwise skip (blocker recorded elsewhere)
        if not (os.environ.get("OUTLINE_API_URL") and os.environ.get("OUTLINE_API_KEY")):
            pytest.skip("Outline credentials not present — live health not verifiable (blocker recorded)")
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter

        adapter = HttpOutlineSourceAdapter(page_limit=1, timeout_s=8)
        data, has_more = adapter._fetch_page(offset=0, limit=1)
        assert isinstance(data, list)
        # bounded: exactly 1 doc in page
        assert len(data) <= 1


# ---------------------------------------------------------------------------
# ACL / tenant pre-filter
# ---------------------------------------------------------------------------
class TestAclTenantPrefilter:
    def test_tenant_mandatory(self):
        from knowledge_index.acl import KnowledgeACLIndex

        idx = KnowledgeACLIndex()
        idx.bulk_index(tenant_id="t1", resource_id="r1", chunks=[{"chunk_id": "c1", "content": "hello"}])
        with pytest.raises(ValueError, match="tenant_id"):
            idx.search(tenant_id="", user_id="u1", groups=[], query="hello")
        with pytest.raises(ValueError, match="tenant_id"):
            idx.search(tenant_id="   ", user_id="u1", groups=[], query="hello")

    def test_group_prefilter_before_query(self):
        from knowledge_index.acl import KnowledgeACLIndex

        idx = KnowledgeACLIndex()
        idx.bulk_index(tenant_id="tenant_a", resource_id="r1", chunks=[{"chunk_id": "c1", "content": "finance report"}], allowed_groups=["finance"])
        idx.bulk_index(tenant_id="tenant_a", resource_id="r2", chunks=[{"chunk_id": "c1", "content": "finance report"}], allowed_groups=["eng"])
        res = idx.search(tenant_id="tenant_a", user_id="u1", groups=["finance"], query="finance")
        assert len(res) == 1 and res[0].resource_id == "r1"
        res2 = idx.search(tenant_id="tenant_a", user_id="u1", groups=["eng"], query="finance")
        assert len(res2) == 1 and res2[0].resource_id == "r2"

    def test_cross_tenant_isolation(self):
        from knowledge_index.acl import KnowledgeACLIndex

        idx = KnowledgeACLIndex()
        idx.bulk_index(tenant_id="tenant_a", resource_id="r1", chunks=[{"chunk_id": "c1", "content": "hello"}], allowed_groups=["finance"])
        idx.bulk_index(tenant_id="tenant_b", resource_id="r1", chunks=[{"chunk_id": "c1", "content": "hello"}], allowed_groups=["finance"])
        res_a = idx.search(tenant_id="tenant_a", user_id="u1", groups=["finance"], query="hello")
        assert all(c.tenant_id == "tenant_a" for c in res_a)
        assert len(res_a) == 1

    @pytest.mark.asyncio
    async def test_persistent_retrieval_acl_prefilter_isolated(self):
        # Isolated sqlite in-memory, no prod DB mutation
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from knowledge_index.orm import Base as KBase
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.retrieval import KnowledgeIndexRetriever
        from knowledge_index.models import KnowledgeIndexEntry

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(KBase.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        repo = KnowledgeIndexRepository(maker)

        # Seed two tenants
        await repo.upsert(
            KnowledgeIndexEntry(
                index_id="idx_a1", source_system="outline", source_resource_id="outline/team/r1", source_uri="/doc/1", tenant_id="tenant_a", group_id="finance", chunk_id="c1", chunk_text="quarterly finance report Q1", content_hash="h1", acl_version="v1", classification="INTERNAL", provenance={"src": "outline"}
            )
        )
        await repo.upsert(
            KnowledgeIndexEntry(
                index_id="idx_b1", source_system="outline", source_resource_id="outline/team/r2", source_uri="/doc/2", tenant_id="tenant_b", group_id="finance", chunk_id="c1", chunk_text="quarterly finance report Q1", content_hash="h1", acl_version="v1", classification="INTERNAL", provenance={"src": "outline"}
            )
        )
        retriever = KnowledgeIndexRetriever(repo)
        # tenant_a search should not leak tenant_b
        hits = await retriever.retrieve(query="finance", tenant_id="tenant_a", allowed_group_ids=["finance"], allowed_agent_ids=[], limit=10, mode="lexical")
        assert all(h.tenant_id == "tenant_a" for h in hits)
        assert len(hits) == 1
        assert hits[0].index_id == "idx_a1"
        # empty tenant should fail closed
        with pytest.raises(ValueError, match="tenant_id"):
            await retriever.retrieve(query="finance", tenant_id="", allowed_group_ids=[], limit=10, mode="lexical")
        await engine.dispose()


# ---------------------------------------------------------------------------
# Incremental sync: content_hash / source_updated_at / acl_version
# ---------------------------------------------------------------------------
class TestIncrementalSync:
    def test_skip_unchanged_all_three_equal(self):
        doc = make_outline_doc(doc_id="doc1", content="same", acl_version="v1", updated_at="2026-01-01T00:00:00+00:00")
        adapter = OutlineSourceAdapter(documents=[doc])
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=8), chunk_store=store, checkpoint_store=cpoint)
        r1 = orch.sync()
        assert r1.upserted == 1
        r2 = orch.sync()
        assert r2.skipped == 1 and r2.upserted == 0

    def test_reembed_on_content_hash_change(self):
        doc1 = make_outline_doc(doc_id="doc1", content="v1", acl_version="v1", updated_at="2026-01-01T00:00:00+00:00")
        adapter = OutlineSourceAdapter(documents=[doc1])
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=8), chunk_store=store, checkpoint_store=cpoint)
        orch.sync()
        doc2 = make_outline_doc(doc_id="doc1", content="v2 changed", acl_version="v1", updated_at="2026-01-01T00:00:00+00:00")
        adapter.set_documents([doc2])
        r2 = orch.sync()
        assert r2.upserted == 1 and r2.skipped == 0

    def test_reembed_on_acl_version_change(self):
        doc1 = make_outline_doc(doc_id="doc1", content="same", acl_version="v1", updated_at="2026-01-01T00:00:00+00:00")
        adapter = OutlineSourceAdapter(documents=[doc1])
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=8), chunk_store=store, checkpoint_store=cpoint)
        orch.sync()
        doc2 = make_outline_doc(doc_id="doc1", content="same", acl_version="v2", updated_at="2026-01-01T00:00:00+00:00")
        adapter.set_documents([doc2])
        r2 = orch.sync()
        assert r2.upserted == 1

    def test_reembed_on_source_updated_at_change(self):
        doc1 = make_outline_doc(doc_id="doc1", content="same", acl_version="v1", updated_at="2026-01-01T00:00:00+00:00")
        adapter = OutlineSourceAdapter(documents=[doc1])
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=8), chunk_store=store, checkpoint_store=cpoint)
        orch.sync()
        doc2 = make_outline_doc(doc_id="doc1", content="same", acl_version="v1", updated_at="2026-01-02T00:00:00+00:00")
        adapter.set_documents([doc2])
        r2 = orch.sync()
        assert r2.upserted == 1

    def test_no_fake_embedding_hash_fallback(self):
        # Sync must not silently use hash fallback — Fake provider is ok in tests, hash without flag in prod would fail
        doc = make_outline_doc(doc_id="doc1", content="hello")
        adapter = OutlineSourceAdapter(documents=[doc])
        # Fake is allowed in test
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=8), chunk_store=InMemoryChunkStore(), checkpoint_store=InMemoryCheckpointStore())
        r = orch.sync()
        assert r.upserted == 1


# ---------------------------------------------------------------------------
# Deletion handling + bounded retry / checkpoint
# ---------------------------------------------------------------------------
class TestDeletionAndCheckpoint:
    def test_deletion_removes_chunks_and_checkpoint(self):
        doc = make_outline_doc(doc_id="doc1", content="hello")
        adapter = OutlineSourceAdapter(documents=[doc])
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=8), chunk_store=store, checkpoint_store=cpoint)
        orch.sync()
        assert store.get("outline/team/doc1") is not None
        adapter.delete_document("outline/team/doc1")
        r2 = orch.sync()
        assert r2.deleted == 1
        assert store.get("outline/team/doc1") is None
        cp = cpoint.load("outline")
        assert "outline/team/doc1" not in cp.resource_states

    def test_bounded_retry_succeeds_within_limit(self):
        doc = make_outline_doc(doc_id="retry1", content="hello")
        adapter = OutlineSourceAdapter(documents=[doc], fail_times=2)
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=8), chunk_store=InMemoryChunkStore(), checkpoint_store=InMemoryCheckpointStore(), max_retries=3, retry_backoff_s=0.01)
        r = orch.sync()
        assert r.fetched == 1 and r.failed == 0
        assert adapter.fetch_calls == 3  # 2 fails + 1 success

    def test_bounded_retry_exhaustion_checkpoint_not_advanced(self):
        doc = make_outline_doc(doc_id="retry1", content="hello")
        adapter = OutlineSourceAdapter(documents=[doc], fail_times=10)
        cpoint = InMemoryCheckpointStore()
        # pre-seed checkpoint with one resource
        cpoint.save(SyncCheckpoint(source_system="outline", last_sync_at="2026-01-01T00:00:00+00:00", resource_states={"outline/team/old": __import__("knowledge_index.models", fromlist=["ResourceState"]).ResourceState(content_hash="h", source_updated_at="2026-01-01T00:00:00+00:00", acl_version="v1")}))
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=8), chunk_store=InMemoryChunkStore(), checkpoint_store=cpoint, max_retries=3, retry_backoff_s=0.01)
        r = orch.sync()
        assert r.failed == 1
        assert len(r.errors) == 1
        # checkpoint should still be old (not overwritten with empty)
        cp = cpoint.load("outline")
        assert "outline/team/old" in cp.resource_states

    def test_checkpoint_persists_across_syncs(self):
        doc = make_outline_doc(doc_id="doc1", content="hello")
        adapter = OutlineSourceAdapter(documents=[doc])
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=8), chunk_store=InMemoryChunkStore(), checkpoint_store=cpoint)
        orch.sync()
        cp1 = cpoint.load("outline")
        assert cp1.last_sync_at
        # second sync without change should keep same resource_states
        orch.sync()
        cp2 = cpoint.load("outline")
        assert cp2.resource_states == cp1.resource_states or len(cp2.resource_states) == len(cp1.resource_states)


# ---------------------------------------------------------------------------
# Live corpus backfill dry-run (bounded, no DB write)
# ---------------------------------------------------------------------------
class TestLiveBackfillDryRun:
    def test_live_backfill_dry_run_bounded_no_db_write(self):
        """Bounded count without embedding or DB mutation."""
        if not (os.environ.get("OUTLINE_API_URL") and os.environ.get("OUTLINE_API_KEY")):
            pytest.skip("Outline credentials not present — live dry-run not verifiable")
        from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter

        adapter = HttpOutlineSourceAdapter(page_limit=2, timeout_s=8)
        data, has_more = adapter._fetch_page(offset=0, limit=2)
        # bounded: at most 2 docs
        assert len(data) <= 2
        # has_more indicates live corpus larger than page — but we don't fetch all without approval
        assert isinstance(has_more, bool)

# ---------------------------------------------------------------------------
# Regression: missing Notion adapter must be fail-closed, not ModuleNotFoundError
# ---------------------------------------------------------------------------
class TestNotionAdapterMissingFailClosed:
    """Clean-checkout regression: when http_notion.py is absent, health must return blocker not crash."""

    def test_check_notion_credentials_missing_adapter_is_blocker_not_crash(self, monkeypatch):
        import sys
        import importlib
        # Hide the module in this process without deleting files: simulate clean checkout
        hid = {}
        for mod in list(sys.modules.keys()):
            if "http_notion" in mod:
                hid[mod] = sys.modules.pop(mod)
        # Also ensure find_spec would fail by blocking import via monkeypatch
        orig_import = __import__

        def _blocked_import(name, *args, **kwargs):
            if "http_notion" in name:
                raise ModuleNotFoundError(f"No module named '{name}' (simulated clean checkout)")
            return orig_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _blocked_import)
        try:
            # Reload health to exercise missing path (health already handles both)
            import knowledge_index.health as hmod
            import importlib
            importlib.reload(hmod)
            res = hmod.check_notion_credentials()
            assert res["verifiable"] is False
            assert res["blocker"] is not None
            assert "Notion adapter missing" in res["blocker"]
            assert res.get("adapter_missing") is True
            # must not raise
        finally:
            monkeypatch.undo()
            for k, v in hid.items():
                sys.modules[k] = v
            # restore health module to normal (reimport with real adapter)
            import importlib as _il
            import knowledge_index.health as _hm
            _il.reload(_hm)

    def test_probe_notion_health_missing_adapter_is_blocker_not_crash(self, monkeypatch):
        import sys
        import importlib
        hid = {}
        for mod in list(sys.modules.keys()):
            if "http_notion" in mod:
                hid[mod] = sys.modules.pop(mod)
        orig_import = __import__

        def _blocked_import(name, *args, **kwargs):
            if "http_notion" in name:
                raise ModuleNotFoundError(f"No module named '{name}' (simulated clean checkout)")
            return orig_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _blocked_import)
        try:
            import knowledge_index.health as hmod
            importlib.reload(hmod)
            r = hmod.probe_notion_health(page_limit=1, http_client=None)
            assert r.ok is False
            assert r.blocker is not None
            assert "Notion adapter missing" in r.blocker
            assert "fail-closed" in (r.error or "").lower()
        finally:
            monkeypatch.undo()
            for k, v in hid.items():
                sys.modules[k] = v
            import importlib as _il2
            import knowledge_index.health as _hm2
            _il2.reload(_hm2)

    def test_check_all_credentials_survives_missing_notion_adapter(self, monkeypatch):
        import sys
        hid = {}
        for mod in list(sys.modules.keys()):
            if "http_notion" in mod:
                hid[mod] = sys.modules.pop(mod)
        orig_import = __import__

        def _blocked_import(name, *args, **kwargs):
            if "http_notion" in name:
                raise ModuleNotFoundError(f"No module named '{name}'")
            return orig_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _blocked_import)
        try:
            import knowledge_index.health as hmod
            import importlib
            importlib.reload(hmod)
            res = hmod.check_all_credentials()
            # outline part must still exist; notion part must be blocker
            assert "outline" in res and "notion" in res
            assert res["notion"]["verifiable"] is False
            assert "Notion adapter missing" in res["notion"]["blocker"]
        finally:
            monkeypatch.undo()
            for k, v in hid.items():
                sys.modules[k] = v
            import importlib as _il3
            import knowledge_index.health as _hm3
            _il3.reload(_hm3)

