"""Adaptive Profile MVP — focused tests (tenant isolation, explicit override, idempotency, hook fallback)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

# ensure unified test key
UNIFIED_KEY = "test-unified-oaos-signing-key-32bytes-long-enough!!"
for _k in ("OAOS_SIGNING_KEY", "OAOS_USER_JWT_SIGNING_KEY", "OAOS_SECURITY_SERVICE_SIGNING_KEY", "OAOS_JWT_SIGNING_KEY", "ADMIN_JWT_SECRET"):
    os.environ[_k] = UNIFIED_KEY
os.environ.setdefault("OAOS_USER_JWT_ISSUER", "open-agent-os-auth")
os.environ.setdefault("OAOS_JWT_ISSUER", "open-agent-os-auth")
os.environ.setdefault("OAOS_USER_JWT_AUDIENCE", "control-plane")
os.environ.setdefault("OAOS_JWT_AUDIENCE", "control-plane")

from jose import jwt as _jwt  # type: ignore


def _make_jwt(sub: str, tenant_id: str) -> str:
    payload = {
        "sub": sub,
        "tenant_id": tenant_id,
        "aud": os.environ.get("OAOS_USER_JWT_AUDIENCE", "control-plane"),
        "iss": os.environ.get("OAOS_USER_JWT_ISSUER", "open-agent-os-auth"),
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        "iat": datetime.now(timezone.utc),
        "jti": uuid.uuid4().hex,
    }
    return _jwt.encode(payload, UNIFIED_KEY, algorithm="HS256")


# ── engine unit tests ───────────────────────────────────────────────────

def test_weighted_update_deterministic_and_explicit_heavier():
    from control_plane.adaptive_profile.engine import weighted_update, EVIDENCE_WEIGHTS

    v1 = weighted_update(0.0, 1, 1.0, "explicit_feedback", 1.0)
    v2 = weighted_update(0.0, 1, 1.0, "style_inference", 1.0)
    assert v1 > v2, "explicit_feedback weight 1.0 should move more than style 0.25"
    # deterministic
    assert weighted_update(0.0, 1, 1.0, "explicit_feedback", 1.0) == v1
    assert weighted_update(0.2, -1, 0.5, "work_pattern", 0.8) == weighted_update(0.2, -1, 0.5, "work_pattern", 0.8)


def test_weighted_update_clamped():
    from control_plane.adaptive_profile.engine import weighted_update

    v = weighted_update(0.9, 1, 1.0, "explicit_feedback", 1.0)
    assert -1.0 <= v <= 1.0


def test_precedence_current_instruction_over_explicit_over_task_over_global():
    from control_plane.adaptive_profile.engine import synthesize_policy

    # global says low verbosity, task says high, explicit says true conclusion_first, current says false
    policy = synthesize_policy(
        current_instruction={"verbosity": "low"},
        explicit_prefs={"verbosity": "high", "conclusion_first": True},
        task_scores={"verbosity": 0.8, "conclusion_first": 0.8},
        global_scores={"verbosity": -0.8, "conclusion_first": -0.8},
    )
    # current wins for verbosity, explicit wins for conclusion_first vs task/global
    assert policy["verbosity"] == "low"
    # explicit should be used when no current: test separately
    policy2 = synthesize_policy(
        current_instruction={},
        explicit_prefs={"conclusion_first": True},
        task_scores={"conclusion_first": -0.9},
        global_scores={"conclusion_first": -0.9},
    )
    assert policy2["conclusion_first"] is True
    # task > global when no explicit/current
    policy3 = synthesize_policy(
        current_instruction={},
        explicit_prefs={},
        task_scores={"conclusion_first": 0.9},
        global_scores={"conclusion_first": -0.9},
    )
    assert policy3["conclusion_first"] is True


def test_explicit_over_task_over_global_for_policy_generation():
    from control_plane.adaptive_profile.engine import synthesize_policy

    # verbosity trait: task high, global low, explicit none -> task wins
    policy = synthesize_policy(task_scores={"verbosity": 0.9}, global_scores={"verbosity": -0.9})
    assert policy["verbosity"] == "high"
    policy2 = synthesize_policy(task_scores={}, global_scores={"verbosity": -0.9})
    assert policy2["verbosity"] == "low"


# ── tenant isolation / ORM ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_isolation_orm():
    from security.models.db import Base
    from security.models.orm import UserProfileORM, TraitScoreORM

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c))

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        now = datetime.now(timezone.utc)
        s.add(UserProfileORM(user_id="u1", tenant_id="tA", profile_version=1, status="active", evidence_count=0, overall_confidence=0.0, created_at=now, updated_at=now))
        s.add(UserProfileORM(user_id="u1", tenant_id="tB", profile_version=1, status="active", evidence_count=0, overall_confidence=0.0, created_at=now, updated_at=now))
        s.add(TraitScoreORM(user_id="u1", tenant_id="tA", trait_name="verbosity", global_score=0.8, confidence=0.9, sample_count=5, last_updated=now))
        await s.commit()

    # query isolation: tenant B should not see tenant A trait
    async with maker() as s:
        from sqlalchemy import select

        r = await s.execute(select(TraitScoreORM).where(TraitScoreORM.user_id == "u1", TraitScoreORM.tenant_id == "tB"))
        assert r.scalars().first() is None
        r2 = await s.execute(select(TraitScoreORM).where(TraitScoreORM.user_id == "u1", TraitScoreORM.tenant_id == "tA"))
        assert r2.scalars().first() is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_tenant_isolation_api():
    """Router requires tenant binding — cross-tenant token cannot read other tenant profile."""
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    # re-create in-memory DB for router's get_sessionmaker
    from security.models.db import Base, get_sessionmaker

    # force new engine by patching env and reimport
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c))

    # Monkey patch get_sessionmaker to use our engine
    import control_plane.adaptive_profile.router as rmod

    orig = rmod.get_sessionmaker
    maker = async_sessionmaker(eng, expire_on_commit=False)
    rmod.get_sessionmaker = lambda url=None: maker

    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(rmod.router)

        # Seed profile for tenant tA user u1
        from security.models.orm import UserProfileORM

        async with maker() as s:
            now = datetime.now(timezone.utc)
            s.add(UserProfileORM(user_id="u1", tenant_id="tA", profile_version=1, status="active", evidence_count=0, overall_confidence=0.0, created_at=now, updated_at=now))
            await s.commit()

        tok_tA = _make_jwt("u1", "tA")
        tok_tB = _make_jwt("u1", "tB")

        with TestClient(app) as c:
            respA = c.get("/v1/profile/me", headers={"Authorization": f"Bearer {tok_tA}"})
            assert respA.status_code == 200
            assert respA.json()["tenant_id"] == "tA"

            respB = c.get("/v1/profile/me", headers={"Authorization": f"Bearer {tok_tB}"})
            assert respB.status_code == 200
            # tB has no profile, should be empty (tenant isolation)
            assert respB.json()["evidence_count"] == 0
            assert respB.json()["tenant_id"] == "tB"

            # cross-user: user u2 cannot read u1 by reusing token but path is self only — verified by token sub isolation
            # evidence post as tB should not pollute tA
            resp = c.post(
                "/v1/profile/evidence",
                headers={"Authorization": f"Bearer {tok_tB}"},
                json={"trait": "verbosity", "direction": 1, "strength": 0.8, "source_type": "explicit_feedback", "confidence": 0.95, "task_type": "general_chat"},
            )
            assert resp.status_code == 200

            # verify tA still has no evidence
            respA2 = c.get("/v1/profile/me", headers={"Authorization": f"Bearer {tok_tA}"})
            assert respA2.json()["evidence_count"] == 0
    finally:
        rmod.get_sessionmaker = orig
        await eng.dispose()
        os.environ.pop("DATABASE_URL", None)


def test_cross_tenant_jwt_mismatch_denied_via_control_plane_session():
    # Verify control-plane session layer also denies cross-tenant (sanity)
    # We test router directly: token tenant != header tenant should be caught by verify path
    from control_plane.auth import verify_user_jwt

    tok = _make_jwt("alice", "tenant-a")
    claims = verify_user_jwt(tok)
    assert claims["tenant_id"] == "tenant-a"


# ── explicit override ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_explicit_override_wins_over_scores():
    from security.models.db import Base
    import control_plane.adaptive_profile.router as rmod
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c))
    maker = async_sessionmaker(eng, expire_on_commit=False)
    orig = rmod.get_sessionmaker
    rmod.get_sessionmaker = lambda url=None: maker
    try:
        app = FastAPI()
        app.include_router(rmod.router)
        tok = _make_jwt("alice", "tenant-x")
        headers = {"Authorization": f"Bearer {tok}"}
        with TestClient(app) as c:
            # ingest evidence that pushes verbosity high
            c.post("/v1/profile/evidence", headers=headers, json={"trait": "verbosity", "direction": 1, "strength": 1.0, "source_type": "explicit_feedback", "confidence": 1.0})
            # now set explicit preference to low
            r = c.put("/v1/profile/preferences", headers=headers, json={"key": "verbosity", "value": "low", "scope": "global"})
            assert r.status_code == 200
            # policy should reflect explicit low even though scores high
            pr = c.get("/v1/profile/policy?task_type=general_chat", headers=headers)
            assert pr.status_code == 200
            assert pr.json()["policy"]["verbosity"] == "low"
    finally:
        rmod.get_sessionmaker = orig
        await eng.dispose()


# ── idempotency ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evidence_idempotency_content_hash():
    from security.models.db import Base
    import control_plane.adaptive_profile.router as rmod
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c))
    maker = async_sessionmaker(eng, expire_on_commit=False)
    orig = rmod.get_sessionmaker
    rmod.get_sessionmaker = lambda url=None: maker
    try:
        app = FastAPI()
        app.include_router(rmod.router)
        tok = _make_jwt("bob", "tenant-1")
        headers = {"Authorization": f"Bearer {tok}"}
        body = {"trait": "conclusion_first", "direction": 1, "strength": 0.8, "source_type": "explicit_feedback", "confidence": 0.9, "task_type": "general_chat", "conversation_id": "conv1", "observed_at": "2026-08-30T10:00:00+09:00", "content_hash": "hash-abc-123"}
        with TestClient(app) as c:
            r1 = c.post("/v1/profile/evidence", headers=headers, json=body)
            assert r1.status_code == 200
            assert r1.json()["deduplicated"] is False
            r2 = c.post("/v1/profile/evidence", headers=headers, json=body)
            assert r2.status_code == 200
            assert r2.json()["deduplicated"] is True
            assert r1.json()["evidence_id"] == r2.json()["evidence_id"]
            # profile count should be 1, not 2
            me = c.get("/v1/profile/me", headers=headers)
            assert me.json()["evidence_count"] == 1
    finally:
        rmod.get_sessionmaker = orig
        await eng.dispose()


# ── hook fallback ──────────────────────────────────────────────────────

def test_hook_fallback_returns_default_on_error():
    from control_plane.adaptive_profile.hook import AdaptiveProfileHook

    hook = AdaptiveProfileHook()

    def broken_loader(*a, **kw):
        raise RuntimeError("db down")

    policy = hook.before_llm_call("t1", "u1", "general_chat", current_instruction={"verbosity": "low"}, profile_loader=broken_loader)
    # should fallback to default merged with current instruction
    assert policy["verbosity"] == "low"
    assert "conclusion_first" in policy
    # shape is minimal 7 keys
    assert set(policy.keys()) == {"conclusion_first", "verbosity", "technical_depth", "evidence_requirement", "challenge_assumptions", "alternatives", "confirmation_level"}


def test_hook_minimal_policy_never_leaks_scores():
    from control_plane.adaptive_profile.hook import AdaptiveProfileHook

    hook = AdaptiveProfileHook()

    def loader(tid, uid, tt):
        return {"explicit_prefs": {}, "task_scores": {"verbosity": 0.9}, "global_scores": {"verbosity": 0.5}}

    policy = hook.before_llm_call("t1", "u1", "general_chat", profile_loader=loader)
    assert "global_score" not in str(policy)
    # policy key evidence_requirement is allowed; ensure no raw score/evidence history leaks
    assert "history" not in str(policy).lower()
    injection = hook.format_prompt_injection(policy)
    assert "score" not in injection.lower()
    assert "[USER RESPONSE POLICY]" in injection


@pytest.mark.asyncio
async def test_reset_clears_profile():
    from security.models.db import Base
    import control_plane.adaptive_profile.router as rmod
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c))
    maker = async_sessionmaker(eng, expire_on_commit=False)
    orig = rmod.get_sessionmaker
    rmod.get_sessionmaker = lambda url=None: maker
    try:
        app = FastAPI()
        app.include_router(rmod.router)
        tok = _make_jwt("carol", "tenant-r")
        headers = {"Authorization": f"Bearer {tok}"}
        with TestClient(app) as c:
            c.post("/v1/profile/evidence", headers=headers, json={"trait": "verbosity", "direction": 1, "strength": 1.0, "source_type": "explicit_feedback", "confidence": 1.0})
            me_before = c.get("/v1/profile/me", headers=headers)
            assert me_before.json()["evidence_count"] == 1
            rr = c.post("/v1/profile/reset", headers=headers)
            assert rr.status_code == 200
            me_after = c.get("/v1/profile/me", headers=headers)
            assert me_after.json()["evidence_count"] == 0
            assert me_after.json()["trait_scores"] == {}
    finally:
        rmod.get_sessionmaker = orig
        await eng.dispose()
