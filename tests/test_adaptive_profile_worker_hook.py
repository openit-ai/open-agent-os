"""Adaptive Profile — Worker + Hook integration (production slice)

FAILING TESTS FIRST — implements TASK: async evidence worker + LLM hook at ACP boundary.

Covers:
- deterministic rule-based extraction (Korean/English phrases)
- worker idempotent persist via profile API/repository
- worker never blocks response path (async, safe exception)
- hook integrated at ACP/Control Plane LLM call boundary with safe fallback and no leakage
"""
from __future__ import annotations
import os
import time
import asyncio
import uuid
from datetime import datetime, timezone, timedelta

UNIFIED_KEY = "test-unified-oaos-signing-key-32bytes-long-enough!!"
for _k in ("OAOS_SIGNING_KEY","OAOS_USER_JWT_SIGNING_KEY","OAOS_SECURITY_SERVICE_SIGNING_KEY","OAOS_JWT_SIGNING_KEY","ADMIN_JWT_SECRET"):
    os.environ[_k] = UNIFIED_KEY
os.environ.setdefault("OAOS_USER_JWT_ISSUER","open-agent-os-auth")
os.environ.setdefault("OAOS_JWT_ISSUER","open-agent-os-auth")
os.environ.setdefault("OAOS_USER_JWT_AUDIENCE","control-plane")
os.environ.setdefault("OAOS_JWT_AUDIENCE","control-plane")

import pytest

# ── extractor deterministic ─────────────────────────────────────────────

def test_extractor_exists():
    from control_plane.adaptive_profile.extractor import extract_evidence
    assert callable(extract_evidence)

def test_extractor_too_long_english():
    from control_plane.adaptive_profile.extractor import extract_evidence
    items = extract_evidence("Your answer was too long, make it shorter")
    # should map to verbosity low (direction -1)
    assert any(i["trait"]=="verbosity" and i["direction"]==-1 for i in items)

def test_extractor_too_long_korean():
    from control_plane.adaptive_profile.extractor import extract_evidence
    items = extract_evidence("너무 길어 간결하게 해줘")
    assert any(i["trait"]=="verbosity" and i["direction"]==-1 for i in items)

def test_extractor_conclusion_first_english():
    from control_plane.adaptive_profile.extractor import extract_evidence
    items = extract_evidence("please conclusion first")
    assert any(i["trait"]=="conclusion_first" and i["direction"]==1 for i in items)

def test_extractor_conclusion_first_korean():
    from control_plane.adaptive_profile.extractor import extract_evidence
    items = extract_evidence("결론부터 말해줘")
    assert any(i["trait"]=="conclusion_first" and i["direction"]==1 for i in items)

def test_extractor_verify_sources():
    from control_plane.adaptive_profile.extractor import extract_evidence
    items = extract_evidence("verify sources and show evidence")
    assert any(i["trait"]=="evidence_requirement" and i["direction"]==1 for i in items)
    items2 = extract_evidence("출처 검증해줘")
    assert any(i["trait"]=="evidence_requirement" and i["direction"]==1 for i in items2)

def test_extractor_proceed_without_asking():
    from control_plane.adaptive_profile.extractor import extract_evidence
    items = extract_evidence("proceed without asking")
    # confirmation_requirement low (-1) or agent_autonomy 1
    assert any(i["trait"] in ("confirmation_requirement","agent_autonomy") and i["direction"] in (-1,1) for i in items)
    items2 = extract_evidence("묻지 말고 진행해")
    assert any(i["trait"] in ("confirmation_requirement","agent_autonomy") for i in items2)

def test_extractor_challenge_assumptions():
    from control_plane.adaptive_profile.extractor import extract_evidence
    items = extract_evidence("challenge assumptions critically")
    assert any(i["trait"]=="critical_challenge" and i["direction"]==1 for i in items)
    items2 = extract_evidence("가정을 반박해줘")
    assert any(i["trait"]=="critical_challenge" and i["direction"]==1 for i in items2)

def test_extractor_no_false_positive():
    from control_plane.adaptive_profile.extractor import extract_evidence
    items = extract_evidence("hello how are you today?")
    assert items == []

def test_extractor_deterministic():
    from control_plane.adaptive_profile.extractor import extract_evidence
    a = extract_evidence("too long, conclusion first")
    b = extract_evidence("too long, conclusion first")
    assert a == b

# ── worker ───────────────────────────────────────────────────────────────

def test_worker_exists_and_nonblocking():
    from control_plane.adaptive_profile.worker import handle_interaction_event, handle_interaction_event_async
    assert callable(handle_interaction_event)
    assert callable(handle_interaction_event_async)

@pytest.mark.asyncio
async def test_worker_idempotent_and_persists():
    """Worker persists via repository (or API) and is idempotent."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from security.models.db import Base
    import control_plane.adaptive_profile.worker as wmod
    import control_plane.adaptive_profile.router as rmod

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c))
    maker = async_sessionmaker(eng, expire_on_commit=False)
    orig = rmod.get_sessionmaker
    rmod.get_sessionmaker = lambda url=None: maker
    orig_worker_maker = getattr(wmod, "get_sessionmaker", None)
    # worker should accept sessionmaker injection or use rmod's
    if hasattr(wmod, "get_sessionmaker"):
        wmod.get_sessionmaker = lambda url=None: maker
    try:
        from control_plane.adaptive_profile.worker import handle_interaction_event_async
        event = {
            "tenant_id": "t1",
            "user_id": "u1",
            "conversation_id": "conv1",
            "message_id": "msg1",
            "task_type": "general_chat",
            "text": "too long, conclusion first please",
            "observed_at": "2026-08-30T10:00:00+09:00",
        }
        r1 = await handle_interaction_event_async(event)
        # should have produced at least one evidence (verbosity + conclusion_first)
        assert r1["processed"] is True
        assert r1["evidence_count"] >= 1
        # second same event (same conversation_id+message_id+text) => deduplicated
        r2 = await handle_interaction_event_async(event)
        assert r2["processed"] is True
        # second call should be deduplicated (no new rows or marked)
        # allow either 0 new or deduplicated flag
        assert r2["deduplicated"] is True or r2["evidence_count"] == r1["evidence_count"]

        # verify DB: at least one evidence row exists
        from sqlalchemy import select
        from security.models.orm import ProfileEvidenceORM
        async with maker() as s:
            res = await s.execute(select(ProfileEvidenceORM).where(ProfileEvidenceORM.user_id=="u1"))
            rows = res.scalars().all()
            assert len(rows) >= 1
            # tenant isolation
            assert all(r.tenant_id=="t1" for r in rows)
    finally:
        rmod.get_sessionmaker = orig
        if orig_worker_maker:
            wmod.get_sessionmaker = orig_worker_maker
        await eng.dispose()

@pytest.mark.asyncio
async def test_worker_never_blocks_response_path():
    """Worker completes fast and never raises to caller (fire-and-forget safe)."""
    import control_plane.adaptive_profile.worker as wmod
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from security.models.db import Base
    import control_plane.adaptive_profile.router as rmod
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c))
    maker = async_sessionmaker(eng, expire_on_commit=False)
    orig = rmod.get_sessionmaker
    rmod.get_sessionmaker = lambda url=None: maker
    if hasattr(wmod, "get_sessionmaker"):
        orig2 = wmod.get_sessionmaker
        wmod.get_sessionmaker = lambda url=None: maker
    else:
        orig2 = None
    try:
        from control_plane.adaptive_profile.worker import handle_interaction_event
        event = {"tenant_id":"t2","user_id":"u2","text":"verify sources","task_type":"general_chat"}
        t0 = time.monotonic()
        # sync wrapper must not raise and must return quickly (<0.5s) even if DB slow
        res = handle_interaction_event(event)
        # handle_interaction_event is sync non-blocking: may return None or result, but must not raise
        dt = time.monotonic() - t0
        assert dt < 0.5, f"blocking too long: {dt}"
        # allow background task to finish
        await asyncio.sleep(0.3)
    finally:
        rmod.get_sessionmaker = orig
        if orig2:
            wmod.get_sessionmaker = orig2
        await eng.dispose()

@pytest.mark.asyncio
async def test_worker_exception_does_not_propagate():
    from control_plane.adaptive_profile.worker import handle_interaction_event_async
    # missing tenant/user should be handled gracefully (no raise)
    bad_event = {"text": "too long"}
    res = await handle_interaction_event_async(bad_event)
    # should return processed=False or error, but not raise
    assert res is not None
    assert "processed" in res

# ── hook integration at LLM boundary ────────────────────────────────────

def test_hook_adapter_seam_exists():
    """Hook exposes adapter seam for Control Plane / ACP integration."""
    from control_plane.adaptive_profile.hook import get_response_policy, AdaptiveProfileHook
    assert callable(get_response_policy)
    hook = AdaptiveProfileHook()
    assert hasattr(hook, "before_llm_call")

def test_acp_adapter_has_policy_integration():
    """ACPAdapter has method to build messages with Response Policy injected."""
    from control_plane.acp_adapter import ACPAdapter
    ad = ACPAdapter("http://localhost:8642")
    # must expose either get_policy or build_messages_with_policy or similar seam
    assert hasattr(ad, "build_llm_messages") or hasattr(ad, "resolve_policy") or hasattr(ad, "_build_messages_with_policy"), \
        "ACPAdapter missing policy integration seam"

@pytest.mark.asyncio
async def test_hook_applied_at_acp_boundary_with_fallback():
    """ACP stream_events injects Response Policy; on failure falls back to default with no leakage."""
    from control_plane.acp_adapter import ACPAdapter
    # We test that adapter's message building includes policy injection and safe fallback
    ad = ACPAdapter("http://localhost:8642")
    # simulate session
    from control_plane.session import SessionRecord
    from datetime import datetime, timezone
    sess = SessionRecord(session_id="sess_test", tenant_id="t1", user_id="u1", agent_id="agent:u1", trace_id="trace1", security_domain="general")
    sess.prompt_history = [{"prompt": "hello"}]
    # If build_llm_messages exists, test it
    if hasattr(ad, "build_llm_messages"):
        msgs = ad.build_llm_messages(sess, "hello", policy={"verbosity":"low","conclusion_first":True,"technical_depth":"medium","evidence_requirement":"medium","challenge_assumptions":False,"alternatives":1,"confirmation_level":"medium"})
        # must contain system prompt with policy hints but no scores
        combined = " ".join(m.get("content","") for m in msgs)
        assert "score" not in combined.lower()
        assert "history" not in combined.lower() or "USER RESPONSE POLICY" in combined
    # safe default: no exception even if policy loader fails
    from control_plane.adaptive_profile.hook import default_hook
    def _broken(*a, **kw):
        raise RuntimeError("db down")
    pol = default_hook.before_llm_call("t1","u1","general_chat", profile_loader=_broken)
    assert set(pol.keys()) == {"conclusion_first","verbosity","technical_depth","evidence_requirement","challenge_assumptions","alternatives","confirmation_level"}
    assert "global_score" not in str(pol)

def test_hook_never_leaks_profile_details():
    from control_plane.adaptive_profile.hook import AdaptiveProfileHook
    hook = AdaptiveProfileHook()
    def loader(tid, uid, tt):
        return {"explicit_prefs": {}, "task_scores": {"verbosity": 0.9, "evidence_requirement": 0.8}, "global_scores": {"conclusion_first": 0.7}}
    pol = hook.before_llm_call("t1","u1","general_chat", profile_loader=loader)
    s = str(pol)
    assert "0.9" not in s  # raw score must not leak
    injection = hook.format_prompt_injection(pol)
    assert "0.9" not in injection
    assert "score" not in injection.lower()
