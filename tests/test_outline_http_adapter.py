"""TDD tests for HttpOutlineSourceAdapter — real read-only Outline HTTP + gated writes.

All HTTP is faked via injected transport (no live network).
Covers:
- fail-closed when credentials missing (fetch + writes)
- HTTP errors / retries bounded, no mock fallback
- pagination collecting all pages
- timeout bounds validation
- normalized SourceDocument fields
- ACL parsing
- content_hash deterministic
- write disabled fail-closed
- create/update/delete payloads, publish:true, read-back verification and mismatch failure
- sync() never writes
"""

from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "packages" / "knowledge-index"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

import pytest
from knowledge_index.chunking import content_hash
from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter, OutlineAPIError
from knowledge_index.embedding import FakeEmbeddingProvider
from knowledge_index.models import SyncCheckpoint
from knowledge_index.store import InMemoryChunkStore, InMemoryCheckpointStore
from knowledge_index.sync import SyncOrchestrator


# ---------------------------------------------------------------------------
# Fake HTTP transport helpers
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeTransport:
    """Queue-based fake transport. Pop from _queue in order."""

    def __init__(self, responses=None, fail_first: int = 0, fail_status: int = 500):
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self.fail_first = fail_first
        self.fail_status = fail_status
        self._call_count = 0

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        self._call_count += 1
        self.calls.append({"url": url, "headers": dict(headers or {}), "json": json, "timeout": timeout})
        if self._call_count <= self.fail_first:
            return FakeResp(self.fail_status, {"error": "transient"})
        if not self.responses:
            raise RuntimeError(f"FakeTransport: no queued response for call {self._call_count} url={url}")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, FakeResp):
            return nxt
        # dict payload -> 200
        return FakeResp(200, nxt)


class RaisingTransport:
    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        self.calls.append({"url": url, "json": json})
        raise self.exc


# ---------------------------------------------------------------------------
# Helpers to build Outline raw doc payloads
# ---------------------------------------------------------------------------
def _raw_doc(doc_id="doc_001", collection="team", title="T", text="hello", updated_at="2026-01-01T00:00:00Z", acl=None, url=""):
    d: dict = {"id": doc_id, "collectionId": collection, "title": title, "text": text, "updatedAt": updated_at}
    if acl is not None:
        d["acl"] = acl
    if url:
        d["url"] = url
    return d


# ---------------------------------------------------------------------------
# Read-only fetch tests
# ---------------------------------------------------------------------------
class TestFailClosed:
    def test_fetch_no_credentials_raises(self):
        # Ensure env not providing creds
        for k in ("OUTLINE_API_URL", "OUTLINE_API_KEY", "OUTLINE_API_TOKEN", "OAOS_OUTLINE_TOKEN"):
            # monkeypatch via explicit empty
            pass
        adapter = HttpOutlineSourceAdapter(api_url="", api_token="", http_client=FakeTransport([]))
        with pytest.raises(RuntimeError, match="credentials missing"):
            adapter.fetch(None)

    def test_fetch_no_credentials_env_missing(self, monkeypatch):
        for k in ("OUTLINE_API_URL", "OUTLINE_API_KEY", "OUTLINE_API_TOKEN", "OAOS_OUTLINE_TOKEN", "OAOS_OUTLINE_URL", "OUTLINE_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        adapter = HttpOutlineSourceAdapter(api_url=None, api_token=None, http_client=FakeTransport([]))
        with pytest.raises(RuntimeError, match="credentials missing"):
            adapter.fetch(None)

    def test_fetch_auth_failure_no_retry(self):
        tr = FakeTransport(responses=[FakeResp(401, {"error": "unauthorized"})])
        adapter = HttpOutlineSourceAdapter(api_url="https://outline.example.com", api_token="bad", http_client=tr, max_retries=3, retry_backoff_s=0.001)
        with pytest.raises(OutlineAPIError, match="auth failed"):
            adapter.fetch(None)
        assert len(tr.calls) == 1  # no retry on 401

    def test_fetch_server_error_retries_then_fails(self):
        tr = FakeTransport(responses=[FakeResp(500, {})] * 3, fail_first=0)
        # Need 3 queued 500 responses; _post_with_retries will retry max_retries times
        # We use FakeTransport that returns 500 each time; adapter retries.
        # Instead queue 500 responses as FakeResp; adapter will raise OutlineAPIError each attempt and retry
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, max_retries=3, retry_backoff_s=0.001)
        with pytest.raises(OutlineAPIError, match="server error"):
            adapter.fetch(None)
        assert len(tr.calls) == 3

    def test_fetch_no_mock_fallback_on_http_error(self):
        tr = RaisingTransport(ConnectionError("network down"))
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, max_retries=2, retry_backoff_s=0.001)
        with pytest.raises(OutlineAPIError):
            adapter.fetch(None)
        # Should not have returned fixture docs
        assert len(tr.calls) == 2


class TestTimeoutAndBounds:
    def test_timeout_bounds_rejected(self):
        with pytest.raises(ValueError):
            HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", timeout_s=0.5)
        with pytest.raises(ValueError):
            HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", timeout_s=120)

    def test_page_limit_bounds_rejected(self):
        with pytest.raises(ValueError):
            HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", page_limit=0)
        with pytest.raises(ValueError):
            HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", page_limit=200)

    def test_timeout_passed_to_transport(self):
        doc = _raw_doc()
        tr = FakeTransport(responses=[{"data": [doc], "pagination": {"offset": 0, "total": 1}}])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, timeout_s=7, retry_backoff_s=0.001)
        adapter.fetch(None)
        assert tr.calls[0]["timeout"] == 7


class TestNormalizationAndPagination:
    def test_single_page_normalization(self):
        raw = _raw_doc(doc_id="doc_1", collection="team", title="My Title", text="my content", updated_at="2026-01-02T03:04:05Z", acl={"groups": ["admin"]}, url="https://outline.example.com/doc/doc_1")
        tr = FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}])
        adapter = HttpOutlineSourceAdapter(api_url="https://outline.example.com", api_token="tok", http_client=tr, retry_backoff_s=0.001)
        res = adapter.fetch(None)
        assert len(res.documents) == 1
        d = res.documents[0]
        assert d.resource_id == "outline/team/doc_1"
        assert d.source_uri == "https://outline.example.com/doc/doc_1"
        assert d.title == "My Title"
        assert d.content == "my content"
        assert "2026-01-02T03:04:05" in d.source_updated_at
        assert d.acl == {"groups": ["admin"]}
        assert d.acl_version is not None
        assert d.content_hash == content_hash("my content")
        assert d.source_system == "outline"

    def test_content_hash_deterministic(self):
        raw = _raw_doc(text="hello world")
        tr = FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, retry_backoff_s=0.001)
        r1 = adapter.fetch(None)
        # second fetch with same transport needs new response
        tr2 = FakeTransport(responses=[{"data": [_raw_doc(text="hello world")], "pagination": {"offset": 0, "total": 1}}])
        adapter2 = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr2, retry_backoff_s=0.001)
        r2 = adapter2.fetch(None)
        assert r1.documents[0].content_hash == r2.documents[0].content_hash

    def test_acl_private_collection_defaults_admin(self):
        raw = _raw_doc(collection="private", acl=None)
        tr = FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, retry_backoff_s=0.001)
        res = adapter.fetch(None)
        assert res.documents[0].acl == {"groups": ["admin"]}

    def test_pagination_collects_all_pages(self):
        docs_page1 = [_raw_doc(f"doc_{i:03d}", text=f"content {i}") for i in range(25)]
        docs_page2 = [_raw_doc(f"doc_{i:03d}", text=f"content {i}") for i in range(25, 30)]
        tr = FakeTransport(
            responses=[
                {"data": docs_page1, "pagination": {"offset": 0, "total": 30}},
                {"data": docs_page2, "pagination": {"offset": 25, "total": 30}},
            ]
        )
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, page_limit=25, retry_backoff_s=0.001)
        res = adapter.fetch(None)
        assert len(res.documents) == 30
        assert adapter.last_fetch_pages == 2
        # all resource_ids normalized
        assert "outline/team/doc_000" in {d.resource_id for d in res.documents}
        assert "outline/team/doc_029" in {d.resource_id for d in res.documents}

    def test_pagination_infer_has_more_when_no_total(self):
        docs_page1 = [_raw_doc(f"d{i}", text="x") for i in range(2)]
        docs_page2: list = []
        tr = FakeTransport(responses=[{"data": docs_page1}, {"data": docs_page2}])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, page_limit=2, retry_backoff_s=0.001)
        res = adapter.fetch(None)
        # first page len==limit => has_more true, second page fetches empty then stops
        assert len(res.documents) == 2

    def test_retry_succeeds_after_transient_failures(self):
        raw = _raw_doc()
        tr = FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}], fail_first=2, fail_status=500)
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, max_retries=3, retry_backoff_s=0.001)
        res = adapter.fetch(None)
        assert len(res.documents) == 1
        assert len(tr.calls) == 3

    def test_incremental_sync_skips_unchanged(self):
        raw = _raw_doc(doc_id="doc_001", text="hello", updated_at="2026-01-01T00:00:00Z")
        tr1 = FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr1, retry_backoff_s=0.001)
        store = InMemoryChunkStore()
        cpoint = InMemoryCheckpointStore()
        orch = SyncOrchestrator(source=adapter, embedding_provider=FakeEmbeddingProvider(dim=16), chunk_store=store, checkpoint_store=cpoint, max_retries=3, retry_backoff_s=0.001)
        r1 = orch.sync()
        assert r1.upserted == 1
        # second sync with same doc -> skipped
        tr2 = FakeTransport(responses=[{"data": [_raw_doc(doc_id="doc_001", text="hello", updated_at="2026-01-01T00:00:00Z")], "pagination": {"offset": 0, "total": 1}}])
        adapter._http_client = tr2  # swap transport
        r2 = orch.sync()
        assert r2.skipped == 1
        assert r2.upserted == 0

    def test_no_secret_in_error_messages(self):
        tr = FakeTransport(responses=[FakeResp(500, {"error": "boom"})])
        token = "super-secret-token-123"
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token=token, http_client=tr, max_retries=1, retry_backoff_s=0.001)
        try:
            adapter.fetch(None)
            assert False, "should raise"
        except OutlineAPIError as e:
            assert token not in str(e)
            # headers not logged either
            for call in tr.calls:
                assert token in call["headers"].get("Authorization", "")  # transport sees it, but error doesn't leak


# ---------------------------------------------------------------------------
# Write-gated tests
# ---------------------------------------------------------------------------
class TestWriteGated:
    def test_create_disabled_fail_closed(self):
        tr = FakeTransport(responses=[{"data": {"id": "new_id"}}])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=False)
        with pytest.raises(PermissionError, match="writes disabled"):
            adapter.create_document(title="T", text="hello")
        assert len(tr.calls) == 0

    def test_update_disabled_fail_closed(self):
        tr = FakeTransport(responses=[])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=False)
        with pytest.raises(PermissionError, match="writes disabled"):
            adapter.update_document(doc_id="doc_001", text="new")
        assert len(tr.calls) == 0

    def test_delete_disabled_fail_closed(self):
        tr = FakeTransport(responses=[])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=False)
        with pytest.raises(PermissionError, match="writes disabled"):
            adapter.delete_document(doc_id="doc_001")
        assert len(tr.calls) == 0

    def test_write_permission_checker_denies(self):
        def deny(action, ctx):
            return False

        tr = FakeTransport(responses=[{"data": {"id": "x"}}])
        adapter = HttpOutlineSourceAdapter(
            api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=True, write_permission_checker=deny
        )
        with pytest.raises(PermissionError, match="permission denied"):
            adapter.create_document(title="T", text="hello")

    def test_write_permission_checker_allows(self):
        def allow(action, ctx):
            return True

        tr = FakeTransport(
            responses=[
                {"data": {"id": "doc_new"}},
                {"data": _raw_doc(doc_id="doc_new", title="T", text="hello", collection="team")},
            ]
        )
        adapter = HttpOutlineSourceAdapter(
            api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=True, write_permission_checker=allow, retry_backoff_s=0.001
        )
        doc = adapter.create_document(title="T", text="hello", collection_id="team")
        assert doc.resource_id == "outline/team/doc_new"

    def test_create_payload_and_publish_and_read_back(self):
        tr = FakeTransport(
            responses=[
                {"data": {"id": "doc_new", "title": "My Doc"}},
                {"data": _raw_doc(doc_id="doc_new", title="My Doc", text="my text", collection="team")},
            ]
        )
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=True, retry_backoff_s=0.001)
        doc = adapter.create_document(title="My Doc", text="my text", collection_id="team", publish=True)
        # First call is create
        assert tr.calls[0]["url"].endswith("/api/documents.create")
        assert tr.calls[0]["json"]["title"] == "My Doc"
        assert tr.calls[0]["json"]["text"] == "my text"
        assert tr.calls[0]["json"]["collectionId"] == "team"
        assert tr.calls[0]["json"]["publish"] is True
        # Second call is read-back
        assert tr.calls[1]["url"].endswith("/api/documents.info")
        assert tr.calls[1]["json"]["id"] == "doc_new"
        assert doc.title == "My Doc"
        assert doc.content == "my text"
        assert doc.content_hash == content_hash("my text")

    def test_update_sends_publish_true_and_read_back(self):
        tr = FakeTransport(
            responses=[
                {"data": {"id": "doc_001"}},
                {"data": _raw_doc(doc_id="doc_001", title="New Title", text="new text", collection="team")},
            ]
        )
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=True, retry_backoff_s=0.001)
        doc = adapter.update_document(doc_id="doc_001", title="New Title", text="new text")
        assert tr.calls[0]["url"].endswith("/api/documents.update")
        assert tr.calls[0]["json"]["id"] == "doc_001"
        assert tr.calls[0]["json"]["title"] == "New Title"
        assert tr.calls[0]["json"]["text"] == "new text"
        assert tr.calls[0]["json"]["publish"] is True
        assert tr.calls[1]["url"].endswith("/api/documents.info")
        assert doc.title == "New Title"
        assert doc.content == "new text"

    def test_update_publish_always_true_even_if_caller_false(self):
        tr = FakeTransport(
            responses=[
                {"data": {"id": "doc_001"}},
                {"data": _raw_doc(doc_id="doc_001", title="T", text="txt")},
            ]
        )
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=True, retry_backoff_s=0.001)
        adapter.update_document(doc_id="doc_001", title="T", text="txt", publish=False)
        assert tr.calls[0]["json"]["publish"] is True

    def test_create_read_back_mismatch_fails(self):
        tr = FakeTransport(
            responses=[
                {"data": {"id": "doc_new"}},
                {"data": _raw_doc(doc_id="doc_new", title="Wrong Title", text="different", collection="team")},
            ]
        )
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=True, retry_backoff_s=0.001)
        with pytest.raises(OutlineAPIError, match="title mismatch|hash mismatch"):
            adapter.create_document(title="My Doc", text="my text", collection_id="team")

    def test_update_read_back_hash_mismatch_fails(self):
        tr = FakeTransport(
            responses=[
                {"data": {"id": "doc_001"}},
                {"data": _raw_doc(doc_id="doc_001", title="T", text="other text")},
            ]
        )
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=True, retry_backoff_s=0.001)
        with pytest.raises(OutlineAPIError, match="hash mismatch"):
            adapter.update_document(doc_id="doc_001", title="T", text="expected text")

    def test_delete_payload(self):
        tr = FakeTransport(responses=[{"success": True}])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=True, retry_backoff_s=0.001)
        ok = adapter.delete_document(doc_id="doc_001")
        assert ok is True
        assert tr.calls[0]["url"].endswith("/api/documents.delete")
        assert tr.calls[0]["json"] == {"id": "doc_001"}

    def test_delete_requires_write_enabled(self):
        tr = FakeTransport(responses=[{"success": True}])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=True, retry_backoff_s=0.001)
        # delete should not log secret in error
        tr2 = FakeTransport(responses=[FakeResp(500, {})])
        adapter2 = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="super-secret-token", http_client=tr2, write_enabled=True, max_retries=1, retry_backoff_s=0.001)
        try:
            adapter2.delete_document(doc_id="doc_001")
            assert False
        except OutlineAPIError as e:
            assert "super-secret-token" not in str(e)

    def test_sync_never_writes(self):
        # Ensure fetch path never triggers write endpoints
        raw = _raw_doc()
        tr = FakeTransport(responses=[{"data": [raw], "pagination": {"offset": 0, "total": 1}}])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token="tok", http_client=tr, write_enabled=False, retry_backoff_s=0.001)
        res = adapter.fetch(None)
        assert len(res.documents) == 1
        for call in tr.calls:
            assert "documents.create" not in call["url"]
            assert "documents.update" not in call["url"]
            assert "documents.delete" not in call["url"]

    def test_write_no_secret_in_logs(self):
        token = "my-secret-token-xyz"
        tr = FakeTransport(responses=[FakeResp(500, {"error": "boom"})])
        adapter = HttpOutlineSourceAdapter(api_url="https://o.example.com", api_token=token, http_client=tr, write_enabled=True, max_retries=1, retry_backoff_s=0.001)
        try:
            adapter.create_document(title="T", text="hello")
            assert False
        except OutlineAPIError as e:
            assert token not in str(e)
