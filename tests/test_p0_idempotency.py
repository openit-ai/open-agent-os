"""P0 Message Execution Reliability — focused regression tests (mock/local/isolated).

Tests run without external Mattermost/Redis: uses fakeredis for atomic claim,
in-memory fallback, and webhook integration with mocked ACPAdapter/MattermostAdapter.

Acceptance mapped:
- tenant+channel+post deterministic key
- Redis atomic claim: 100 concurrent same post_id → 1 LLM call equivalent
- duplicate read-back, response_post_id stored
- bridge 2 simultaneous (same key, concurrent threads)
- CP restart recovery (redis persistence vs in-memory loss)
- Redis failure production fail-closed 503, non-prod fallback
- 429/timeout bounded retry → retryable fail allows reclaim
- audit contains idempotency_key
"""
from __future__ import annotations

import asyncio
import os
import threading
import uuid
import pytest

def _fakeredis_client():
    try:
        import fakeredis  # type: ignore
        return fakeredis.FakeRedis(decode_responses=True)
    except Exception as e:
        pytest.skip(f"fakeredis not available: {e}")

# ── unit: deterministic key ───────────────────────────────────────────────────
class TestIdempotencyKey:
    def test_deterministic(self):
        from control_plane.idempotency import build_idempotency_key
        k1 = build_idempotency_key("tenantA", "chan1", "post123")
        k2 = build_idempotency_key("tenantA", "chan1", "post123")
        k3 = build_idempotency_key("tenantA", "chan2", "post123")
        k4 = build_idempotency_key("tenantB", "chan1", "post123")
        assert k1 == k2
        assert k1 != k3
        assert k1 != k4
        assert k1 is not None and k1.startswith("oaos:mm:idem:")

    def test_no_post_id_returns_none(self):
        from control_plane.idempotency import build_idempotency_key
        assert build_idempotency_key("t", "c", None) is None
        assert build_idempotency_key("t", "c", "") is None

    def test_prefix_hash_length(self):
        from control_plane.idempotency import build_idempotency_key
        k = build_idempotency_key("t", "c", "p1")
        assert len(k.split(":")[-1]) == 32

# ── unit: atomic claim with fakeredis ────────────────────────────────────────
class TestAtomicClaim:
    def setup_method(self):
        from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client
        clear_inmem_store()
        clear_idempotency_redis_client()
        for env in ("OAOS_ENV","OAOS_ALLOW_TEST_FALLBACK","OAOS_CP_REDIS_URL","OAOS_REDIS_URL","REDIS_URL"):
            os.environ.pop(env, None)

    def teardown_method(self):
        from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client
        clear_inmem_store()
        clear_idempotency_redis_client()
        for env in ("OAOS_ENV","OAOS_ALLOW_TEST_FALLBACK","OAOS_CP_REDIS_URL","OAOS_REDIS_URL","REDIS_URL"):
            os.environ.pop(env, None)

    def test_100_concurrent_single_claim(self):
        r = _fakeredis_client()
        from control_plane.idempotency import set_idempotency_redis_client, build_idempotency_key, try_claim, clear_inmem_store
        set_idempotency_redis_client(r)
        clear_inmem_store()
        tenant, chan, post = "t-conc-100", "c1", f"p-{uuid.uuid4().hex[:8]}"
        results = []
        barrier = threading.Barrier(20)  # 20 threads each 5 attempts? Use 100 threads but barrier 100 may time out; use 20
        # 100 total claims via 20 threads *5 loop
        def worker():
            try:
                barrier.wait(timeout=2)
                for _ in range(5):
                    _, res = try_claim(tenant_id=tenant, channel_id=chan, post_id=post, session_id=f"s-{threading.get_ident()}", trace_id="tr", request_id="req")
                    results.append(res.status if res else "none")
            except Exception as e:
                results.append(f"err:{e}")
        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=5)
        claimed = results.count("claimed") + results.count("reclaimed")
        duplicates = results.count("duplicate_processing") + results.count("duplicate_completed")
        assert claimed == 1, f"expected 1 claimed got {claimed} results sample {results[:20]}"
        assert duplicates == 99

    def test_duplicate_completed_read_back(self):
        r = _fakeredis_client()
        from control_plane.idempotency import set_idempotency_redis_client, try_claim, complete, get_record
        set_idempotency_redis_client(r)
        tenant, chan, post = "t-dup", "c1", f"p-{uuid.uuid4().hex[:6]}"
        _, cl1 = try_claim(tenant_id=tenant, channel_id=chan, post_id=post, session_id="s1", trace_id="tr1", request_id="r1")
        assert cl1.status == "claimed"
        key = cl1.key
        complete(key, response_post_id="mm-post-123", session_id="s1", trace_id="tr1")
        _, cl2 = try_claim(tenant_id=tenant, channel_id=chan, post_id=post, session_id="s2", trace_id="tr2", request_id="r2")
        assert cl2.status == "duplicate_completed"
        assert cl2.record.get("response_post_id") == "mm-post-123"
        assert cl2.is_duplicate is True
        rec = get_record(key)
        assert rec["response_post_id"] == "mm-post-123"

    def test_complete_stores_response_post_id(self):
        r = _fakeredis_client()
        from control_plane.idempotency import set_idempotency_redis_client, try_claim, complete, get_record
        set_idempotency_redis_client(r)
        _, c = try_claim(tenant_id="t", channel_id="c", post_id="p-test-store", session_id="s", trace_id="tr", request_id="rq")
        complete(c.key, response_post_id="resp-post-xyz")
        rec = get_record(c.key)
        assert rec["status"] == "completed"
        assert rec["response_post_id"] == "resp-post-xyz"

    def test_retryable_failed_allows_reclaim(self):
        r = _fakeredis_client()
        from control_plane.idempotency import set_idempotency_redis_client, try_claim, fail
        set_idempotency_redis_client(r)
        tenant, chan, post = "t-retry", "c1", f"p-{uuid.uuid4().hex[:6]}"
        _, c1 = try_claim(tenant_id=tenant, channel_id=chan, post_id=post, session_id="s1", trace_id="tr1", request_id="r1")
        assert c1.status == "claimed"
        fail(c1.key, error="429 quota exceeded", retryable=True)
        _, c2 = try_claim(tenant_id=tenant, channel_id=chan, post_id=post, session_id="s2", trace_id="tr2", request_id="r2")
        assert c2.status == "reclaimed"
        assert not c2.is_duplicate

    def test_non_retryable_failed_blocks_reclaim(self):
        r = _fakeredis_client()
        from control_plane.idempotency import set_idempotency_redis_client, try_claim, fail
        set_idempotency_redis_client(r)
        tenant, chan, post = "t-nonretry", "c1", f"p-{uuid.uuid4().hex[:6]}"
        _, c1 = try_claim(tenant_id=tenant, channel_id=chan, post_id=post, session_id="s1", trace_id="tr1", request_id="r1")
        fail(c1.key, error="policy denied", retryable=False)
        _, c2 = try_claim(tenant_id=tenant, channel_id=chan, post_id=post, session_id="s2", trace_id="tr2", request_id="r2")
        assert c2.is_duplicate

# ── prod fail-closed / non-prod fallback ─────────────────────────────────────
class TestProdFailClosed:
    def teardown_method(self):
        for k in ("OAOS_ENV","OAOS_ALLOW_TEST_FALLBACK","OAOS_CP_REDIS_URL","OAOS_REDIS_URL"):
            os.environ.pop(k, None)
        try:
            from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client
            clear_inmem_store(); clear_idempotency_redis_client()
        except: pass

    def test_prod_redis_down_503(self):
        from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client
        clear_inmem_store(); clear_idempotency_redis_client()
        os.environ["OAOS_ENV"] = "production"
        os.environ["OAOS_CP_REDIS_URL"] = "redis://127.0.0.1:59999/0"
        from control_plane.idempotency import try_claim
        with pytest.raises(Exception) as ei:
            try_claim(tenant_id="t", channel_id="c", post_id="p-prod-fail", session_id="s", trace_id="tr", request_id="rq")
        assert getattr(ei.value, "status_code", None) == 503 or "unavailable" in str(getattr(ei.value, "detail", ei.value)).lower()

    def test_nonprod_fallback_allows_claim(self):
        from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client
        clear_inmem_store(); clear_idempotency_redis_client()
        os.environ.pop("OAOS_ENV", None)
        os.environ["OAOS_CP_REDIS_URL"] = "redis://127.0.0.1:59999/0"
        from control_plane.idempotency import try_claim
        _, res = try_claim(tenant_id="t", channel_id="c", post_id="p-nonprod-fallback", session_id="s", trace_id="tr", request_id="rq")
        assert res is not None
        assert res.status in ("claimed","reclaimed")

    def test_prod_allow_test_fallback(self):
        from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client
        clear_inmem_store(); clear_idempotency_redis_client()
        os.environ["OAOS_ENV"] = "production"
        os.environ["OAOS_ALLOW_TEST_FALLBACK"] = "1"
        os.environ["OAOS_CP_REDIS_URL"] = "redis://127.0.0.1:59999/0"
        from control_plane.idempotency import try_claim
        _, res = try_claim(tenant_id="t", channel_id="c", post_id="p-allow-fallback", session_id="s", trace_id="tr", request_id="rq")
        assert res is not None

# ── restart recovery (redis persistence) ─────────────────────────────────────
class TestRestartRecovery:
    def test_redis_persistence_across_restart(self):
        r = _fakeredis_client()
        from control_plane.idempotency import set_idempotency_redis_client, try_claim, complete, clear_inmem_store, get_record
        set_idempotency_redis_client(r)
        clear_inmem_store()
        tenant, chan, post = "t-restart", "c1", f"p-{uuid.uuid4().hex[:6]}"
        _, c1 = try_claim(tenant_id=tenant, channel_id=chan, post_id=post, session_id="s1", trace_id="tr1", request_id="r1")
        complete(c1.key, response_post_id="mm-post-restart-1")
        # simulate CP restart: clear in-memory but keep redis
        clear_inmem_store()
        # new process still sees completed via redis
        _, c2 = try_claim(tenant_id=tenant, channel_id=chan, post_id=post, session_id="s2", trace_id="tr2", request_id="r2")
        assert c2.status == "duplicate_completed"
        assert c2.record["response_post_id"] == "mm-post-restart-1"

# ── webhook integration (mocked ACP/Mattermost) ──────────────────────────────
class TestWebhookIdempotency:
    def setup_method(self):
        for k in ("OAOS_ENV","OAOS_ALLOW_TEST_FALLBACK","OAOS_CP_REDIS_URL"):
            os.environ.pop(k, None)
        from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client
        clear_inmem_store(); clear_idempotency_redis_client()
        # ensure session store is fresh (in-memory fallback)
        try:
            from control_plane.session import get_session_store
            # not resetting; create new ids enough
        except: pass

    def test_webhook_duplicate_suppresses_llm_second_call(self):
        r = _fakeredis_client()
        from control_plane.idempotency import set_idempotency_redis_client
        set_idempotency_redis_client(r)
        from unittest.mock import AsyncMock, patch
        import control_plane.mattermost_adapter.webhook as wh

        call_count = {"n": 0}
        async def fake_send_prompt(self, rec, text, rid, **kwargs):
            call_count["n"] += 1
            return {"status": "queued", "request_id": rid}
        async def fake_stream(self, rec):
            # simple one-token done
            yield {"type": "token", "data": {"text": "hello"}, "trace_id": getattr(rec,"trace_id","")}
            yield {"type": "done"}

        # patch ACPAdapter and MattermostAdapter
        with patch.object(wh.ACPAdapter, "send_prompt", fake_send_prompt), \
             patch.object(wh.ACPAdapter, "stream_events", fake_stream), \
             patch("control_plane.mattermost_adapter.webhook._get_mattermost_adapter", return_value=None):
            async def run():
                res1 = await wh._handle_core_logic(tenant_id="t1", user_id="employee:kim", text="hi there", session_id=None, channel_id="chanA", post_id="post-dedup-001")
                res2 = await wh._handle_core_logic(tenant_id="t1", user_id="employee:kim", text="hi there", session_id=None, channel_id="chanA", post_id="post-dedup-001")
                return res1, res2
            res1, res2 = asyncio.run(run())
        assert res1["received"] is True
        assert res2.get("duplicate") is True
        assert res2.get("idempotency_key")
        # only first call hit ACP
        assert call_count["n"] == 1
        # second returns read-back response_post_id field (may be empty until stream complete, but duplicate flag present)
        assert res2.get("idempotency_status") in ("duplicate_processing","duplicate_completed")

    def test_webhook_returns_idempotency_key_on_success(self):
        r = _fakeredis_client()
        from control_plane.idempotency import set_idempotency_redis_client
        set_idempotency_redis_client(r)
        from unittest.mock import patch
        import control_plane.mattermost_adapter.webhook as wh
        async def fake_send_prompt(self, rec, text, rid, **kwargs):
            return {"status": "queued"}
        async def fake_stream(self, rec):
            yield {"type": "done"}
        with patch.object(wh.ACPAdapter, "send_prompt", fake_send_prompt), \
             patch.object(wh.ACPAdapter, "stream_events", fake_stream), \
             patch("control_plane.mattermost_adapter.webhook._get_mattermost_adapter", return_value=None):
            async def run():
                return await wh._handle_core_logic(tenant_id="t1", user_id="employee:kim", text="hello", session_id=None, channel_id="chanB", post_id="post-key-001")
            res = asyncio.run(run())
        assert "idempotency_key" in res
        assert res["idempotency_key"].startswith("oaos:mm:idem:")

    def test_webhook_no_post_id_no_idempotency(self):
        from unittest.mock import patch
        import control_plane.mattermost_adapter.webhook as wh
        async def fake_send_prompt(self, rec, text, rid, **kwargs):
            return {"status": "queued"}
        async def fake_stream(self, rec):
            yield {"type":"done"}
        with patch.object(wh.ACPAdapter, "send_prompt", fake_send_prompt), \
             patch.object(wh.ACPAdapter, "stream_events", fake_stream), \
             patch("control_plane.mattermost_adapter.webhook._get_mattermost_adapter", return_value=None):
            async def run():
                return await wh._handle_core_logic(tenant_id="t1", user_id="employee:kim", text="hello", session_id=None, channel_id="chanB", post_id=None)
            res = asyncio.run(run())
        # when no post_id, idempotency_key is "" (no gate)
        assert res.get("idempotency_key") == ""

    def test_post_with_retry_bounded_three_attempts(self):
        from unittest.mock import AsyncMock
        import control_plane.mattermost_adapter.webhook as wh
        adapter = AsyncMock()
        adapter.send_message = AsyncMock(side_effect=[Exception("timeout"), Exception("timeout"), {"id": "post-ok"}])
        async def run():
            pid = await wh._post_with_retry(adapter, "chan", "hello", "root1", "trace1", "sess1")
            return pid
        pid = asyncio.run(run())
        assert pid == "post-ok"
        assert adapter.send_message.call_count == 3

    def test_post_with_retry_gives_up_after_three(self):
        from unittest.mock import AsyncMock
        import control_plane.mattermost_adapter.webhook as wh
        adapter = AsyncMock()
        adapter.send_message = AsyncMock(side_effect=Exception("timeout"))
        async def run():
            pid = await wh._post_with_retry(adapter, "chan", "hello", "root1", "trace1", "sess1")
            return pid
        pid = asyncio.run(run())
        assert pid == ""
        assert adapter.send_message.call_count == 3

# ── P0 hardening focused additions (fail-closed on state update, marker, partial) ──
class TestIdempotencyStateUpdateFailClosed:
    def teardown_method(self):
        for k in ("OAOS_ENV","OAOS_ALLOW_TEST_FALLBACK","OAOS_CP_REDIS_URL","OAOS_REDIS_URL"):
            os.environ.pop(k, None)
        try:
            from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client
            clear_inmem_store(); clear_idempotency_redis_client()
        except: pass

    def test_complete_prod_redis_down_503(self):
        from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client, build_idempotency_key
        clear_inmem_store(); clear_idempotency_redis_client()
        os.environ["OAOS_ENV"] = "production"
        os.environ["OAOS_CP_REDIS_URL"] = "redis://127.0.0.1:59999/0"
        from control_plane.idempotency import complete
        key = build_idempotency_key("t","c","p-complete-fail")
        with pytest.raises(Exception) as ei:
            complete(key, response_post_id="x", response_marker="m")
        assert getattr(ei.value, "status_code", None) == 503 or "unavailable" in str(getattr(ei.value, "detail", ei.value)).lower()

    def test_fail_prod_redis_down_503(self):
        from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client, build_idempotency_key
        clear_inmem_store(); clear_idempotency_redis_client()
        os.environ["OAOS_ENV"] = "production"
        os.environ["OAOS_CP_REDIS_URL"] = "redis://127.0.0.1:59999/0"
        from control_plane.idempotency import fail, build_idempotency_key
        key = build_idempotency_key("t","c","p-fail-fail")
        with pytest.raises(Exception) as ei:
            fail(key, error="oops", retryable=True)
        assert getattr(ei.value, "status_code", None) == 503 or "unavailable" in str(getattr(ei.value, "detail", ei.value)).lower()

    def test_get_record_prod_redis_down_503(self):
        from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client, build_idempotency_key
        clear_inmem_store(); clear_idempotency_redis_client()
        os.environ["OAOS_ENV"] = "production"
        os.environ["OAOS_CP_REDIS_URL"] = "redis://127.0.0.1:59999/0"
        from control_plane.idempotency import get_record, build_idempotency_key
        key = build_idempotency_key("t","c","p-get-fail")
        with pytest.raises(Exception) as ei:
            get_record(key)
        assert getattr(ei.value, "status_code", None) == 503 or "unavailable" in str(getattr(ei.value, "detail", ei.value)).lower()

    def test_complete_nonprod_fallback_does_not_raise(self):
        from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client, build_idempotency_key, try_claim, complete, get_record
        clear_inmem_store(); clear_idempotency_redis_client()
        os.environ.pop("OAOS_ENV", None)
        os.environ["OAOS_CP_REDIS_URL"] = "redis://127.0.0.1:59999/0"
        _, cl = try_claim(tenant_id="t", channel_id="c", post_id="p-nonprod-complete", session_id="s", trace_id="tr", request_id="rq")
        # non-prod complete should fallback to inmem, not raise
        complete(cl.key, response_post_id="mm-1", response_marker="mk")
        rec = get_record(cl.key)
        assert rec["status"] == "completed"

    def test_fail_stores_partial_ids(self):
        r = _fakeredis_client()
        from control_plane.idempotency import set_idempotency_redis_client, try_claim, fail, get_record, clear_inmem_store
        set_idempotency_redis_client(r); clear_inmem_store()
        _, c = try_claim(tenant_id="t", channel_id="c", post_id=f"p-partial-{uuid.uuid4().hex[:4]}", session_id="s", trace_id="tr", request_id="rq")
        fail(c.key, error="delivery failed", retryable=True, response_post_ids=["a","b"], response_marker="mm")
        rec = get_record(c.key)
        assert rec["status"] == "failed"
        assert rec.get("response_post_ids") == ["a","b"]
        assert rec.get("response_marker") == "mm"


class TestHasDuplicateMarkerMismatch:
    def setup_method(self):
        for k in ("OAOS_ENV","OAOS_ALLOW_TEST_FALLBACK","OAOS_CP_REDIS_URL"):
            os.environ.pop(k, None)
        from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client
        clear_inmem_store(); clear_idempotency_redis_client()
    def teardown_method(self):
        for k in ("OAOS_ENV","OAOS_ALLOW_TEST_FALLBACK","OAOS_CP_REDIS_URL"):
            os.environ.pop(k, None)
        from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client
        clear_inmem_store(); clear_idempotency_redis_client()

    def test_marker_mismatch_not_duplicate(self):
        r = _fakeredis_client()
        from control_plane.idempotency import set_idempotency_redis_client, try_claim, complete, has_duplicate_response, build_response_marker
        set_idempotency_redis_client(r)
        _, c = try_claim(tenant_id="t", channel_id="chan", post_id="p-marker-mismatch", session_id="s", trace_id="tr", request_id="rq")
        # complete with marker based on full text
        full = "hello world full response text"
        marker = build_response_marker("chan", None, full)
        complete(c.key, response_post_id="mm-1", response_marker=marker)
        is_dup, rec = has_duplicate_response(c.key, marker=marker)
        assert is_dup is True
        # mismatch marker should be NOT duplicate
        other_marker = build_response_marker("chan", None, "different content")
        is_dup2, rec2 = has_duplicate_response(c.key, marker=other_marker)
        assert is_dup2 is False
        assert rec2 is not None
        assert rec2["response_marker"] == marker

    def test_without_marker_still_duplicate(self):
        r = _fakeredis_client()
        from control_plane.idempotency import set_idempotency_redis_client, try_claim, complete, has_duplicate_response
        set_idempotency_redis_client(r)
        _, c = try_claim(tenant_id="t", channel_id="c2", post_id="p-marker-none", session_id="s", trace_id="tr", request_id="rq")
        complete(c.key, response_post_id="mm-2", response_marker="mk2")
        is_dup, _ = has_duplicate_response(c.key)
        assert is_dup is True
        is_dup_none, _ = has_duplicate_response(c.key, marker=None)
        assert is_dup_none is True


class TestStreamMarkerFullText:
    def test_build_marker_full_text_deterministic(self):
        from control_plane.idempotency import build_response_marker
        from control_plane.mattermost_adapter.webhook import _build_response_marker
        # both helpers should produce same for same inputs
        m1 = build_response_marker("chanA", "root1", "hello world "*20)
        m2 = _build_response_marker("hello world "*20, "chanA", "root1")
        assert m1 == m2
        assert len(m1) == 16
        # empty vs full should differ
        m_empty = build_response_marker("chanA", "root1", "")
        assert m_empty != m1

    def test_complete_marker_from_full_text(self):
        r = _fakeredis_client()
        from control_plane.idempotency import set_idempotency_redis_client, try_claim, complete, get_record, build_response_marker
        set_idempotency_redis_client(r)
        _, c = try_claim(tenant_id="t", channel_id="chanX", post_id="p-fulltext-marker", session_id="s", trace_id="tr", request_id="rq")
        full_text = "a"*500 + "\nmore"
        marker = build_response_marker("chanX", "rootY", full_text)
        complete(c.key, response_post_id="mm-xyz", response_post_ids=["mm-xyz"], response_marker=marker)
        rec = get_record(c.key)
        assert rec["response_marker"] == marker
        # marker must be deterministic on full text, not empty buffer
        empty_marker = build_response_marker("chanX", "rootY", "")
        assert rec["response_marker"] != empty_marker


class TestWebhookDuplicateNoNewSession:
    def setup_method(self):
        for k in ("OAOS_ENV","OAOS_ALLOW_TEST_FALLBACK","OAOS_CP_REDIS_URL"):
            os.environ.pop(k, None)
        from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client
        clear_inmem_store(); clear_idempotency_redis_client()
        from control_plane.session import session_store
        # clear inmem sessions
        try:
            session_store._store.clear()
        except: pass

    def teardown_method(self):
        from control_plane.idempotency import clear_inmem_store, clear_idempotency_redis_client
        clear_inmem_store(); clear_idempotency_redis_client()

    def test_duplicate_does_not_create_new_session(self):
        r = _fakeredis_client()
        from control_plane.idempotency import set_idempotency_redis_client
        set_idempotency_redis_client(r)
        from unittest.mock import patch
        from control_plane.mattermost_adapter import webhook as wh
        ss = wh.session_store
        if hasattr(ss, "_store"):
            ss._store.clear()

        call_count = {"n": 0, "sessions_before": 0, "sessions_after": 0}
        async def fake_send_prompt(self, rec, text, rid, **kwargs):
            call_count["n"] += 1
            return {"status": "queued"}
        async def fake_stream(self, rec):
            yield {"type": "done"}

        with patch.object(wh.ACPAdapter, "send_prompt", fake_send_prompt), \
             patch.object(wh.ACPAdapter, "stream_events", fake_stream), \
             patch("control_plane.mattermost_adapter.webhook._get_mattermost_adapter", return_value=None):
            async def run():
                # first call creates session
                before = len(ss._store) if hasattr(ss, "_store") else 0
                res1 = await wh._handle_core_logic(tenant_id="t1", user_id="employee:kim", text="hi", session_id=None, channel_id="chanA", post_id="post-no-newsess-001")
                after1 = len(ss._store) if hasattr(ss, "_store") else 0
                res2 = await wh._handle_core_logic(tenant_id="t1", user_id="employee:kim", text="hi", session_id=None, channel_id="chanA", post_id="post-no-newsess-001")
                after2 = len(ss._store) if hasattr(ss, "_store") else 0
                return (res1, res2, before, after1, after2)
            res1, res2, before, after1, after2 = asyncio.run(run())
        assert res1["received"] is True
        assert res2.get("duplicate") is True
        assert call_count["n"] == 1
        # only one new session created for both calls (duplicate did not create second)
        assert after1 == before + 1
        assert after2 == after1

