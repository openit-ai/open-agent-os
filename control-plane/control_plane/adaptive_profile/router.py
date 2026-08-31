"""Profile API — self-scope only, tenant+user isolated.

Mount in control-plane FastAPI as:
  from control_plane.adaptive_profile.router import router as profile_router
  app.include_router(profile_router)

All endpoints require verified JWT. Cross-tenant/cross-user access is 403.
"""
from __future__ import annotations

import hashlib
import uuid
import os
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Depends, Request
from pydantic import BaseModel

from security.models.db import get_sessionmaker
from security.models.orm import (
    UserProfileORM,
    TraitScoreORM,
    TaskTraitScoreORM,
    ProfileEvidenceORM,
    ExplicitPreferenceORM,
)
from .engine import (
    TRAITS,
    TASK_TYPES,
    EVIDENCE_WEIGHTS,
    DEFAULT_POLICY,
    EVIDENCE_TYPE_ALIASES,
    VALID_SOURCE_TYPES,
    weighted_update,
    compute_confidence,
    synthesize_policy,
    content_hash as engine_content_hash,
    validate_task_type,
    validate_trait,
)
from .hook import default_hook
from .cache import get_cached_policy, set_cached_policy, invalidate_user_cache
from .projection import project_behavior

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/profile", tags=["adaptive-profile"])


# ── helpers: verified identity (reuses control-plane auth) ──────────────

def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        if os.getenv(k, "").strip().lower() in ("production", "prod"):
            return True
    return False


def _allow_test_fixture() -> bool:
    if _is_production():
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    flag = os.environ.get("OAOS_ALLOW_TEST_FIXTURE", "") or os.environ.get("OAOS_ALLOW_TEST_FALLBACK", "")
    if flag.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if os.environ.get("PYTEST_RUN", "").lower() in ("1", "true"):
        return True
    return False


def _resolve_identity(authorization: str | None, x_user_id: str | None) -> tuple[str, str]:
    """Return (tenant_id, user_id) from verified JWT or test fixture."""
    token = None
    if authorization:
        auth = authorization.strip()
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        elif auth:
            raise HTTPException(status_code=401, detail="invalid bearer: expected 'Bearer <token>'")
    if token:
        try:
            # reuse control-plane verifier for consistency
            from control_plane.auth import verify_user_jwt  # type: ignore

            claims = verify_user_jwt(token)
            tid = str(claims.get("tenant_id") or claims.get("tenant") or "")
            uid = str(claims.get("sub") or "")
            if not tid or not uid:
                raise HTTPException(status_code=401, detail="missing tenant_id/sub")
            if x_user_id and x_user_id != uid:
                raise HTTPException(status_code=401, detail="IDENTITY_MISMATCH: X-User-Id != token.sub")
            return tid, uid
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"invalid token: {e}")
    # Non-JWT path: only allowed in non-prod test fixture
    if _allow_test_fixture() and x_user_id:
        # Use X-Tenant-Id header or default tenant for tests
        # Caller must supply tenant via header when using fixture
        return "test-tenant", x_user_id
    raise HTTPException(status_code=401, detail="missing bearer token")


async def _require_self(authorization: str | None = Header(default=None, alias="Authorization"), x_user_id: str | None = Header(default=None, alias="X-User-Id"), x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id")):
    """FastAPI dependency — returns (tenant_id, user_id)."""
    tid, uid = _resolve_identity(authorization, x_user_id)
    # When test fixture with explicit X-Tenant-Id, honor it
    if _allow_test_fixture() and x_tenant_id:
        tid = x_tenant_id
    return tid, uid


# ── request models ───────────────────────────────────────────────────────

class PreferencePut(BaseModel):
    key: str
    value: Any
    scope: str = "global"  # global | task
    task_type: str | None = None
    priority: int = 100


class EvidencePost(BaseModel):
    trait: str
    direction: int
    strength: float
    source_type: str
    confidence: float
    task_type: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    observed_at: str | None = None  # ISO8601; defaults to now
    content_hash: str | None = None  # optional idempotency key; if omitted, computed deterministically


# ── GET /projection ──────────────────────────────────────────────────────

@router.get("/projection")
async def get_profile_projection(auth=Depends(_require_self)):
    """Return non-diagnostic behavior projection for the authenticated user."""
    tenant_id, user_id = auth
    maker = get_sessionmaker()
    async with maker() as session:
        from sqlalchemy import select
        profile = await session.get(UserProfileORM, {"user_id": user_id, "tenant_id": tenant_id})
        res = await session.execute(select(TraitScoreORM).where(TraitScoreORM.user_id == user_id, TraitScoreORM.tenant_id == tenant_id))
        traits = {r.trait_name: {"score": r.global_score, "confidence": r.confidence, "sample_count": r.sample_count} for r in res.scalars().all()}
    result = project_behavior(traits)
    return {"tenant_id": tenant_id, "user_id": user_id, "profile_version": profile.profile_version if profile else 0, **result}


# ── GET /me ──────────────────────────────────────────────────────────────

@router.get("/me")
async def get_my_profile(auth=Depends(_require_self)):
    tenant_id, user_id = auth
    maker = get_sessionmaker()
    async with maker() as session:
        from sqlalchemy import select

        profile = await session.get(UserProfileORM, {"user_id": user_id, "tenant_id": tenant_id})
        if not profile:
            return {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "profile_version": 0,
                "status": "none",
                "evidence_count": 0,
                "overall_confidence": 0.0,
                "trait_scores": {},
                "task_scores": {},
                "explicit_preferences": [],
            }
        # trait scores
        res = await session.execute(select(TraitScoreORM).where(TraitScoreORM.user_id == user_id, TraitScoreORM.tenant_id == tenant_id))
        traits = {r.trait_name: {"score": r.global_score, "confidence": r.confidence, "sample_count": r.sample_count} for r in res.scalars().all()}
        res2 = await session.execute(select(TaskTraitScoreORM).where(TaskTraitScoreORM.user_id == user_id, TaskTraitScoreORM.tenant_id == tenant_id))
        task_map: dict[str, dict] = {}
        for r in res2.scalars().all():
            task_map.setdefault(r.task_type, {})[r.trait_name] = {"score": r.score, "confidence": r.confidence, "sample_count": r.sample_count}
        res3 = await session.execute(select(ExplicitPreferenceORM).where(ExplicitPreferenceORM.user_id == user_id, ExplicitPreferenceORM.tenant_id == tenant_id))
        prefs = [{"key": r.key, "value": r.value, "scope": r.scope, "task_type": r.task_type, "priority": r.priority} for r in res3.scalars().all()]
        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "profile_version": profile.profile_version,
            "status": profile.status,
            "evidence_count": profile.evidence_count,
            "overall_confidence": profile.overall_confidence,
            "trait_scores": traits,
            "task_scores": task_map,
            "explicit_preferences": prefs,
        }


# ── GET /policy ──────────────────────────────────────────────────────────

@router.get("/policy")
async def get_response_policy(task_type: str | None = None, auth=Depends(_require_self)):
    """Return minimal Response Policy for caller (self). Redis cached."""
    tenant_id, user_id = auth
    task_type = validate_task_type(task_type)
    maker = get_sessionmaker()
    async with maker() as session:
        from sqlalchemy import select

        prof = await session.get(UserProfileORM, {"user_id": user_id, "tenant_id": tenant_id})
        profile_version = prof.profile_version if prof else 0

        # try cache before DB scores
        try:
            cached = get_cached_policy(tenant_id, user_id, task_type, profile_version)
            if cached is not None and "policy" in cached:
                pc = {k: cached["policy"][k] for k in DEFAULT_POLICY.keys() if k in cached["policy"]}
                return {"tenant_id": tenant_id, "user_id": user_id, "task_type": task_type, "policy": pc, "profile_version": profile_version}
        except Exception:
            pass

        # load preferences + scores
        res3 = await session.execute(select(ExplicitPreferenceORM).where(ExplicitPreferenceORM.user_id == user_id, ExplicitPreferenceORM.tenant_id == tenant_id))
        explicit: dict[str, Any] = {}
        for r in res3.scalars().all():
            if r.scope == "global":
                explicit[r.key] = _coerce_pref_value(r.value)
            elif r.task_type == task_type:
                explicit[r.key] = _coerce_pref_value(r.value)

        res = await session.execute(select(TraitScoreORM).where(TraitScoreORM.user_id == user_id, TraitScoreORM.tenant_id == tenant_id))
        global_scores = {r.trait_name: r.global_score for r in res.scalars().all()}
        res2 = await session.execute(select(TaskTraitScoreORM).where(TaskTraitScoreORM.user_id == user_id, TaskTraitScoreORM.tenant_id == tenant_id, TaskTraitScoreORM.task_type == task_type))
        task_scores = {r.trait_name: r.score for r in res2.scalars().all()}

    try:
        policy = synthesize_policy(explicit_prefs=explicit, task_scores=task_scores, global_scores=global_scores)
    except Exception:
        policy = dict(DEFAULT_POLICY)
    policy = {k: policy[k] for k in DEFAULT_POLICY.keys() if k in policy}
    try:
        set_cached_policy(tenant_id, user_id, task_type, profile_version, policy)
    except Exception:
        pass
    return {"tenant_id": tenant_id, "user_id": user_id, "task_type": task_type, "policy": policy, "profile_version": profile_version}


def _coerce_pref_value(v: Any) -> Any:
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        if s.isdigit() or (s.lstrip("-").isdigit()):
            return int(s)
    except Exception:
        pass
    try:
        f = float(s)
        if "." in s:
            return f
    except Exception:
        pass
    return s


# ── PUT /preferences ─────────────────────────────────────────────────────

@router.put("/preferences")
async def put_preference(body: PreferencePut, auth=Depends(_require_self)):
    tenant_id, user_id = auth
    if body.scope not in ("global", "task"):
        raise HTTPException(status_code=400, detail="scope must be global|task")
    if body.scope == "task" and not body.task_type:
        raise HTTPException(status_code=400, detail="task_type required for task scope")
    if body.key not in ("conclusion_first", "verbosity", "technical_depth", "evidence_requirement", "challenge_assumptions", "alternatives", "confirmation_level"):
        raise HTTPException(status_code=400, detail=f"unknown preference key: {body.key}")
    maker = get_sessionmaker()
    async with maker() as session:
        from sqlalchemy import select

        pref_id = hashlib.sha256(f"{tenant_id}|{user_id}|{body.key}|{body.scope}|{body.task_type or ''}".encode()).hexdigest()[:16]
        pref_id = f"pref_{pref_id}"
        now = datetime.now(timezone.utc)
        existing = await session.get(ExplicitPreferenceORM, pref_id)
        if existing:
            if existing.tenant_id != tenant_id or existing.user_id != user_id:
                raise HTTPException(status_code=403, detail="cross-tenant preference denied")
            existing.value = str(body.value)
            existing.priority = body.priority
            existing.updated_at = now
        else:
            row = ExplicitPreferenceORM(
                preference_id=pref_id,
                user_id=user_id,
                tenant_id=tenant_id,
                scope=body.scope,
                task_type=validate_task_type(body.task_type) if body.scope == "task" else None,
                key=body.key,
                value=str(body.value),
                priority=body.priority,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        prof = await session.get(UserProfileORM, {"user_id": user_id, "tenant_id": tenant_id})
        if not prof:
            prof = UserProfileORM(user_id=user_id, tenant_id=tenant_id, profile_version=1, status="active", evidence_count=0, overall_confidence=0.0, created_at=now, updated_at=now)
            session.add(prof)
        else:
            prof.profile_version += 1
            prof.updated_at = now
        await session.commit()
        logger.info("audit profile preference tenant=%s user=%s key=%s scope=%s", tenant_id, user_id, body.key, body.scope)
    try:
        invalidate_user_cache(tenant_id, user_id)
    except Exception:
        pass
    return {"preference_id": pref_id, "key": body.key, "value": str(body.value), "scope": body.scope, "task_type": body.task_type}


# ── POST /evidence ───────────────────────────────────────────────────────

@router.post("/evidence")
async def post_evidence(body: EvidencePost, auth=Depends(_require_self)):
    tenant_id, user_id = auth
    if body.trait not in TRAITS:
        raise HTTPException(status_code=400, detail=f"unknown trait: {body.trait}")
    if body.direction not in (-1, 1):
        raise HTTPException(status_code=400, detail="direction must be -1 or 1")
    if not (0.0 <= body.strength <= 1.0) or not (0.0 <= body.confidence <= 1.0):
        raise HTTPException(status_code=400, detail="strength/confidence must be in [0,1]")
    st = body.source_type
    if st in EVIDENCE_TYPE_ALIASES:
        st = EVIDENCE_TYPE_ALIASES[st]
    if st not in VALID_SOURCE_TYPES:
        raise HTTPException(status_code=400, detail=f"unknown source_type: {body.source_type}")

    task_type = validate_task_type(body.task_type)
    observed = body.observed_at or datetime.now(timezone.utc).isoformat()
    try:
        observed_dt = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except Exception:
        observed_dt = datetime.now(timezone.utc)

    ch = body.content_hash
    if not ch:
        ch = engine_content_hash(tenant_id, user_id, body.trait, body.direction, st, observed, body.conversation_id or "")

    maker = get_sessionmaker()
    async with maker() as session:
        from sqlalchemy import select

        res = await session.execute(select(ProfileEvidenceORM).where(ProfileEvidenceORM.content_hash == ch))
        existing_ev = res.scalars().first()
        if existing_ev:
            if existing_ev.tenant_id != tenant_id or existing_ev.user_id != user_id:
                raise HTTPException(status_code=403, detail="cross-tenant evidence denied")
            return {"evidence_id": existing_ev.evidence_id, "content_hash": ch, "deduplicated": True}

        ev_id = f"ev_{uuid.uuid4().hex[:16]}"
        ev = ProfileEvidenceORM(
            evidence_id=ev_id,
            user_id=user_id,
            tenant_id=tenant_id,
            conversation_id=body.conversation_id,
            message_id=body.message_id,
            task_type=task_type,
            trait=body.trait,
            direction=body.direction,
            strength=body.strength,
            source_type=st,
            confidence=body.confidence,
            observed_at=observed_dt,
            content_hash=ch,
        )
        session.add(ev)

        now = datetime.now(timezone.utc)
        gs = await session.get(TraitScoreORM, {"user_id": user_id, "tenant_id": tenant_id, "trait_name": body.trait})
        old = gs.global_score if gs else 0.0
        new_score = weighted_update(old, body.direction, body.strength, st, body.confidence)
        if gs:
            gs.global_score = new_score
            gs.sample_count += 1
            gs.confidence = compute_confidence(gs.sample_count)
            gs.last_updated = now
        else:
            gs = TraitScoreORM(user_id=user_id, tenant_id=tenant_id, trait_name=body.trait, global_score=new_score, confidence=compute_confidence(1), sample_count=1, last_updated=now)
            session.add(gs)

        ts = await session.get(TaskTraitScoreORM, {"user_id": user_id, "tenant_id": tenant_id, "task_type": task_type, "trait_name": body.trait})
        old_t = ts.score if ts else 0.0
        new_t = weighted_update(old_t, body.direction, body.strength, st, body.confidence)
        if ts:
            ts.score = new_t
            ts.sample_count += 1
            ts.confidence = compute_confidence(ts.sample_count)
            ts.last_updated = now
        else:
            ts = TaskTraitScoreORM(user_id=user_id, tenant_id=tenant_id, task_type=task_type, trait_name=body.trait, score=new_t, confidence=compute_confidence(1), sample_count=1, last_updated=now)
            session.add(ts)

        prof = await session.get(UserProfileORM, {"user_id": user_id, "tenant_id": tenant_id})
        if not prof:
            prof = UserProfileORM(user_id=user_id, tenant_id=tenant_id, profile_version=1, status="active", evidence_count=1, overall_confidence=compute_confidence(1), created_at=now, updated_at=now)
            session.add(prof)
        else:
            prof.evidence_count += 1
            prof.profile_version += 1
            prof.overall_confidence = compute_confidence(prof.evidence_count)
            prof.updated_at = now

        await session.commit()
        logger.info("audit profile evidence tenant=%s user=%s trait=%s dir=%s", tenant_id, user_id, body.trait, body.direction)
    try:
        invalidate_user_cache(tenant_id, user_id)
    except Exception:
        pass
    return {"evidence_id": ev_id, "content_hash": ch, "deduplicated": False, "global_score": new_score, "task_score": new_t}


# ── POST /reset ──────────────────────────────────────────────────────────

@router.post("/reset")
async def reset_profile(auth=Depends(_require_self)):
    tenant_id, user_id = auth
    maker = get_sessionmaker()
    async with maker() as session:
        from sqlalchemy import delete, select

        await session.execute(delete(TraitScoreORM).where(TraitScoreORM.user_id == user_id, TraitScoreORM.tenant_id == tenant_id))
        await session.execute(delete(TaskTraitScoreORM).where(TaskTraitScoreORM.user_id == user_id, TaskTraitScoreORM.tenant_id == tenant_id))
        await session.execute(delete(ProfileEvidenceORM).where(ProfileEvidenceORM.user_id == user_id, ProfileEvidenceORM.tenant_id == tenant_id))
        await session.execute(delete(ExplicitPreferenceORM).where(ExplicitPreferenceORM.user_id == user_id, ExplicitPreferenceORM.tenant_id == tenant_id))
        prof = await session.get(UserProfileORM, {"user_id": user_id, "tenant_id": tenant_id})
        if prof:
            prof.profile_version += 1
            prof.evidence_count = 0
            prof.overall_confidence = 0.0
            prof.status = "active"
            prof.updated_at = datetime.now(timezone.utc)
        else:
            now = datetime.now(timezone.utc)
            prof = UserProfileORM(user_id=user_id, tenant_id=tenant_id, profile_version=1, status="active", evidence_count=0, overall_confidence=0.0, created_at=now, updated_at=now)
            session.add(prof)
        await session.commit()
        logger.info("audit profile reset tenant=%s user=%s", tenant_id, user_id)
    try:
        invalidate_user_cache(tenant_id, user_id)
    except Exception:
        pass
    return {"status": "reset", "tenant_id": tenant_id, "user_id": user_id}


# Also expose self-check helper for isolation tests
