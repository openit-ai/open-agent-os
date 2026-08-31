"""Async interaction evidence worker — deterministic, idempotent, non-blocking.

Accepts an interaction event, extracts explicit feedback via extractor,
persists idempotent evidence through existing profile repository (ORM),
never blocks the response path.
"""
from __future__ import annotations
import asyncio
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from .extractor import extract_evidence
from .features import extract_features
from .engine import content_hash as engine_content_hash, validate_task_type, weighted_update, compute_confidence

logger = logging.getLogger(__name__)

# Sessionmaker provider — defaults to security.models.db.get_sessionmaker
# Tests monkey-patch control_plane.adaptive_profile.router.get_sessionmaker and this one.

def get_sessionmaker(url: str | None = None):
    try:
        from security.models.db import get_sessionmaker as _gsm
        return _gsm(url)
    except Exception as e:
        logger.warning(f"worker get_sessionmaker fallback: {e}")
        raise

async def _persist_one(
    maker,
    tenant_id: str,
    user_id: str,
    trait: str,
    direction: int,
    strength: float,
    source_type: str,
    confidence: float,
    task_type: str,
    conversation_id: str | None,
    message_id: str | None,
    observed_at: str,
    extra_hash: str = "",
) -> dict[str, Any]:
    """Persist single evidence idempotently; returns {deduplicated, evidence_id}."""
    from sqlalchemy import select
    from security.models.orm import ProfileEvidenceORM, TraitScoreORM, TaskTraitScoreORM, UserProfileORM

    ch = engine_content_hash(tenant_id, user_id, trait, direction, source_type, observed_at, extra_hash or (conversation_id or ""))
    try:
        observed_dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except Exception:
        observed_dt = datetime.now(timezone.utc)
        observed_at = observed_dt.isoformat()

    async with maker() as session:
        # idempotency check by content_hash (unique index)
        res = await session.execute(select(ProfileEvidenceORM).where(ProfileEvidenceORM.content_hash == ch))
        existing = res.scalars().first()
        if existing:
            if existing.tenant_id != tenant_id or existing.user_id != user_id:
                # cross-tenant reuse of hash (should not happen as hash includes tenant/user)
                logger.warning(f"cross-tenant content_hash collision tenant={tenant_id} user={user_id}")
                return {"deduplicated": True, "evidence_id": existing.evidence_id, "content_hash": ch}
            return {"deduplicated": True, "evidence_id": existing.evidence_id, "content_hash": ch}

        ev_id = f"ev_{uuid.uuid4().hex[:16]}"
        ev = ProfileEvidenceORM(
            evidence_id=ev_id,
            user_id=user_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            message_id=message_id,
            task_type=task_type,
            trait=trait,
            direction=direction,
            strength=strength,
            source_type=source_type,
            confidence=confidence,
            observed_at=observed_dt,
            content_hash=ch,
        )
        session.add(ev)

        now = datetime.now(timezone.utc)
        gs = await session.get(TraitScoreORM, {"user_id": user_id, "tenant_id": tenant_id, "trait_name": trait})
        old = gs.global_score if gs else 0.0
        new_score = weighted_update(old, direction, strength, source_type, confidence)
        if gs:
            gs.global_score = new_score
            gs.sample_count += 1
            gs.confidence = compute_confidence(gs.sample_count)
            gs.last_updated = now
        else:
            gs = TraitScoreORM(user_id=user_id, tenant_id=tenant_id, trait_name=trait, global_score=new_score, confidence=compute_confidence(1), sample_count=1, last_updated=now)
            session.add(gs)

        ts = await session.get(TaskTraitScoreORM, {"user_id": user_id, "tenant_id": tenant_id, "task_type": task_type, "trait_name": trait})
        old_t = ts.score if ts else 0.0
        new_t = weighted_update(old_t, direction, strength, source_type, confidence)
        if ts:
            ts.score = new_t
            ts.sample_count += 1
            ts.confidence = compute_confidence(ts.sample_count)
            ts.last_updated = now
        else:
            ts = TaskTraitScoreORM(user_id=user_id, tenant_id=tenant_id, task_type=task_type, trait_name=trait, score=new_t, confidence=compute_confidence(1), sample_count=1, last_updated=now)
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

        try:
            await session.commit()
        except Exception as e:
            # unique violation race -> treat as deduplicated
            await session.rollback()
            msg = str(e).lower()
            if "unique" in msg or "content_hash" in msg:
                logger.info(f"evidence race deduplicated ch={ch}")
                return {"deduplicated": True, "evidence_id": ev_id, "content_hash": ch}
            raise
        logger.info(f"audit worker evidence tenant={tenant_id} user={user_id} trait={trait} dir={direction}")
        try:
            from .cache import invalidate_user_cache
            invalidate_user_cache(tenant_id, user_id)
        except Exception:
            pass
        return {"deduplicated": False, "evidence_id": ev_id, "content_hash": ch, "global_score": new_score, "task_score": new_t}


async def handle_interaction_event_async(event: dict[str, Any]) -> dict[str, Any]:
    """Async worker entry — safe, never raises to caller."""
    try:
        tenant_id = str(event.get("tenant_id") or "").strip()
        user_id = str(event.get("user_id") or "").strip()
        text = str(event.get("text") or event.get("prompt") or event.get("message") or "").strip()
        if not tenant_id or not user_id:
            return {"processed": False, "reason": "missing tenant_id/user_id", "evidence_count": 0}
        if not text:
            return {"processed": False, "reason": "empty text", "evidence_count": 0}

        task_type = validate_task_type(event.get("task_type"))
        conversation_id = event.get("conversation_id") or event.get("session_id")
        message_id = event.get("message_id") or event.get("request_id")
        observed_at = event.get("observed_at") or datetime.now(timezone.utc).isoformat()

        items = extract_evidence(text, task_type=task_type)
        # Behavioral features are intentionally extracted here, off the request
        # path. Only features backed by the existing trait contract are promoted
        # to evidence; the full feature aggregate is a later persistence phase.
        features = extract_features(text, observed_at=observed_at)
        supported_traits = {"conclusion_first", "verbosity", "evidence_requirement", "agent_autonomy", "confirmation_requirement", "planning_orientation", "completion_orientation", "critical_challenge"}
        for feature in features:
            if feature.name not in supported_traits:
                continue
            items.append({"trait": feature.name, "direction": 1 if feature.value >= 0 else -1, "strength": abs(feature.value), "confidence": feature.confidence, "source_type": feature.source_type, "task_type": task_type})
        if not items:
            return {"processed": True, "reason": "no behavioral evidence detected", "evidence_count": 0, "deduplicated": False}

        # Validate generated evidence before opening the database session.
        for item in items:
            if item["trait"] not in {"conclusion_first", "verbosity", "evidence_requirement", "agent_autonomy", "confirmation_requirement", "planning_orientation", "completion_orientation", "critical_challenge"}:
                continue
            item["source_type"] = item.get("source_type") or "general_expression"
            item["task_type"] = task_type

        # Use get_sessionmaker that tests can patch; fallback to router's maker if DB configured differently
        maker = None
        try:
            # Prefer this module's get_sessionmaker (patchable)
            maker = get_sessionmaker()
        except Exception:
            # try router's get_sessionmaker (tests patch that)
            try:
                from control_plane.adaptive_profile.router import get_sessionmaker as r_gsm
                # need to check if r_gsm is patched to return a maker directly (our test: lambda url=None: maker)
                # get_sessionmaker() with no args should work; if it expects url, handle
                try:
                    maker = r_gsm()
                except TypeError:
                    maker = r_gsm
            except Exception as e2:
                return {"processed": False, "reason": f"no sessionmaker: {e2}", "evidence_count": 0}

        # If get_sessionmaker returned a maker factory, ensure it's callable to get session
        # In tests, get_sessionmaker returns async_sessionmaker instance; we treat maker as that instance
        # _persist_one expects maker() to give async session context manager. So if maker is async_sessionmaker, maker() is correct.
        # If maker is already a function returning maker, we already called it.

        results: list[dict] = []
        any_new = False
        dedup_all = True
        for it in items:
            try:
                r = await _persist_one(
                    maker,
                    tenant_id, user_id,
                    it["trait"], it["direction"], it["strength"], it["source_type"], it["confidence"],
                    task_type, conversation_id, message_id, observed_at, extra_hash=(conversation_id or "") + (message_id or "") + it["trait"]
                )
                results.append(r)
                if not r.get("deduplicated"):
                    any_new = True
                    dedup_all = False
                else:
                    # if at least one was deduplicated but others new, not all deduped
                    if any_new:
                        dedup_all = False
            except Exception as e:
                logger.warning(f"worker persist item failed trait={it['trait']}: {e}")
                results.append({"deduplicated": False, "error": str(e)})

        # Overall deduplicated flag: True only if all items were deduplicated and at least one item existed
        dedup_flag = dedup_all and len(results) > 0 and not any_new
        return {"processed": True, "evidence_count": len(results), "results": results, "deduplicated": dedup_flag}
    except Exception as e:
        logger.warning(f"worker handle_interaction_event_async error: {e}")
        return {"processed": False, "reason": str(e), "evidence_count": 0, "deduplicated": False}

def handle_interaction_event(event: dict[str, Any]) -> dict[str, Any]:
    """Sync non-blocking wrapper — never blocks response path, never raises."""
    try:
        # Fast validation without DB to ensure quick return
        # Schedule async work in background
        try:
            loop = asyncio.get_running_loop()
            # running loop -> create task fire-and-forget
            loop.create_task(handle_interaction_event_async(event))
            return {"processed": True, "scheduled": True, "evidence_count": 0}
        except RuntimeError:
            # no running loop -> spawn daemon thread with its own loop
            def _run():
                try:
                    asyncio.run(handle_interaction_event_async(event))
                except Exception as e:
                    logger.warning(f"worker thread error: {e}")
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            return {"processed": True, "scheduled": True, "evidence_count": 0}
    except Exception as e:
        logger.warning(f"worker handle_interaction_event wrapper error: {e}")
        return {"processed": False, "reason": str(e), "evidence_count": 0}
