"""Adaptive Profile Runtime Hook — minimal Response Policy with Redis cache."""
from __future__ import annotations

import os
import logging
from typing import Any
from .engine import DEFAULT_POLICY, TASK_TYPES, synthesize_policy, validate_task_type

logger = logging.getLogger(__name__)


class AdaptiveProfileHook:
    """Runtime Hook interface.

    before_llm_call: given verified agent context (tenant_id/user_id/task_type),
    returns minimal Response Policy dict (7 keys, no scores/evidence).
    Fail-safe: on any error returns DEFAULT_POLICY.
    after_interaction: async evidence ingestion trigger (MVP: direct call).
    """

    def before_llm_call(
        self,
        tenant_id: str,
        user_id: str,
        task_type: str | None = None,
        current_instruction: dict[str, Any] | None = None,
        profile_loader: Any | None = None,
    ) -> dict[str, Any]:
        """Emit minimal Response Policy.

        profile_loader: optional callable (tenant_id, user_id, task_type) -> {explicit, task_scores, global_scores}
        If None or fails, falls back to DEFAULT_POLICY merged with current_instruction.
        """
        task_type = validate_task_type(task_type)
        cur = current_instruction or {}
        if profile_loader is None:
            merged = dict(DEFAULT_POLICY)
            for k in merged:
                if k in cur:
                    merged[k] = cur[k]
            return merged
        try:
            data = profile_loader(tenant_id, user_id, task_type)
            explicit = {}
            task_scores = {}
            global_scores = {}
            if isinstance(data, dict):
                explicit = data.get("explicit_prefs") or data.get("explicit") or {}
                task_scores = data.get("task_scores") or {}
                global_scores = data.get("global_scores") or data.get("trait_scores") or {}
            policy = synthesize_policy(
                current_instruction=cur,
                explicit_prefs=explicit,
                task_scores=task_scores,
                global_scores=global_scores,
            )
            allowed = set(DEFAULT_POLICY.keys())
            return {k: v for k, v in policy.items() if k in allowed}
        except Exception as e:
            logger.warning(f"AdaptiveProfileHook before_llm_call fallback: {e}")
            merged = dict(DEFAULT_POLICY)
            for k in merged:
                if k in cur:
                    merged[k] = cur[k]
            return merged

    def format_prompt_injection(self, policy: dict[str, Any]) -> str:
        """Format minimal policy for LLM context injection (no scores)."""
        lines = ["[USER RESPONSE POLICY]"]
        if policy.get("conclusion_first"):
            lines.append("- 결론을 먼저 제시")
        if policy.get("verbosity") == "low":
            lines.append("- 간결하게 응답")
        elif policy.get("verbosity") == "high":
            lines.append("- 충분히 상세하게 응답")
        else:
            lines.append("- 적절한 길이로 응답")
        td = policy.get("technical_depth", "medium")
        if td == "high":
            lines.append("- 기술적 깊이 높게 설명")
        elif td == "low":
            lines.append("- 비기술적/개요 중심으로 설명")
        er = policy.get("evidence_requirement", "medium")
        if er == "high":
            lines.append("- 근거가 필요한 사실은 검증")
        if policy.get("challenge_assumptions"):
            lines.append("- 필요한 경우 기존 가정을 반박")
        alt = policy.get("alternatives", 1)
        if alt > 1:
            lines.append(f"- 핵심 대안은 {alt}개 이하로 제시")
        cl = policy.get("confirmation_level", "medium")
        if cl == "low":
            lines.append("- 불필요한 확인 질문 최소화")
        elif cl == "high":
            lines.append("- 실행 전 확인 질문 포함")
        return "\n".join(lines)


def _sync_profile_loader(tenant_id: str, user_id: str, task_type: str) -> dict:
    """Attempt to load profile data synchronously via async DB (best-effort).
    Returns {} on failure so caller falls back to DEFAULT_POLICY.
    Never raises, never leaks scores directly.
    """
    try:
        # try cache first
        try:
            from .cache import get_cached_policy
            # need profile_version to key; fetch version via quick DB check or skip
            # we cannot get version without DB, so cache lookup here uses version-aware path only via hook's version loader
            # For sync loader, we attempt to load version first
            import asyncio
            # Check if we can get version without full load
            pass
        except Exception:
            pass
        import asyncio

        async def _load():
            try:
                from security.models.db import get_sessionmaker
                from security.models.orm import ExplicitPreferenceORM, TraitScoreORM, TaskTraitScoreORM
                from sqlalchemy import select

                maker = get_sessionmaker()
                async with maker() as session:
                    res3 = await session.execute(select(ExplicitPreferenceORM).where(ExplicitPreferenceORM.user_id == user_id, ExplicitPreferenceORM.tenant_id == tenant_id))
                    explicit: dict = {}
                    for r in res3.scalars().all():
                        if r.scope == "global":
                            explicit[r.key] = r.value
                        elif r.task_type == task_type:
                            explicit[r.key] = r.value
                        v = explicit.get(r.key)
                        if isinstance(v, str):
                            if v.lower() in ("true", "false"):
                                explicit[r.key] = v.lower() == "true"
                            elif v.isdigit():
                                try:
                                    explicit[r.key] = int(v)
                                except Exception:
                                    pass
                    res = await session.execute(select(TraitScoreORM).where(TraitScoreORM.user_id == user_id, TraitScoreORM.tenant_id == tenant_id))
                    global_scores = {r.trait_name: r.global_score for r in res.scalars().all()}
                    res2 = await session.execute(select(TaskTraitScoreORM).where(TaskTraitScoreORM.user_id == user_id, TaskTraitScoreORM.tenant_id == tenant_id, TaskTraitScoreORM.task_type == task_type))
                    task_scores = {r.trait_name: r.score for r in res2.scalars().all()}
                    return {"explicit_prefs": explicit, "task_scores": task_scores, "global_scores": global_scores}
            except Exception as e:
                logger.debug(f"_sync_profile_loader load failed: {e}")
                return {"explicit_prefs": {}, "task_scores": {}, "global_scores": {}}

        try:
            asyncio.get_running_loop()
            return {"explicit_prefs": {}, "task_scores": {}, "global_scores": {}}
        except RuntimeError:
            pass
        return asyncio.run(_load())
    except Exception as e:
        logger.debug(f"_sync_profile_loader outer fail: {e}")
        return {"explicit_prefs": {}, "task_scores": {}, "global_scores": {}}


async def _async_profile_loader(tenant_id: str, user_id: str, task_type: str) -> dict:
    """Async DB loader for use inside async ACP path."""
    try:
        from security.models.db import get_sessionmaker
        from security.models.orm import ExplicitPreferenceORM, TraitScoreORM, TaskTraitScoreORM
        from sqlalchemy import select

        maker = get_sessionmaker()
        async with maker() as session:
            res3 = await session.execute(select(ExplicitPreferenceORM).where(ExplicitPreferenceORM.user_id == user_id, ExplicitPreferenceORM.tenant_id == tenant_id))
            explicit: dict = {}
            for r in res3.scalars().all():
                if r.scope == "global":
                    explicit[r.key] = r.value
                elif r.task_type == task_type:
                    explicit[r.key] = r.value
                v = explicit.get(r.key)
                if isinstance(v, str):
                    if v.lower() in ("true", "false"):
                        explicit[r.key] = v.lower() == "true"
                    elif v.isdigit():
                        try:
                            explicit[r.key] = int(v)
                        except Exception:
                            pass
            res = await session.execute(select(TraitScoreORM).where(TraitScoreORM.user_id == user_id, TraitScoreORM.tenant_id == tenant_id))
            global_scores = {r.trait_name: r.global_score for r in res.scalars().all()}
            res2 = await session.execute(select(TaskTraitScoreORM).where(TaskTraitScoreORM.user_id == user_id, TaskTraitScoreORM.tenant_id == tenant_id, TaskTraitScoreORM.task_type == task_type))
            task_scores = {r.trait_name: r.score for r in res2.scalars().all()}
            return {"explicit_prefs": explicit, "task_scores": task_scores, "global_scores": global_scores}
    except Exception as e:
        logger.debug(f"_async_profile_loader failed: {e}")
        return {"explicit_prefs": {}, "task_scores": {}, "global_scores": {}}


def get_response_policy(
    tenant_id: str,
    user_id: str,
    task_type: str | None = None,
    current_instruction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Synchronous adapter seam — Control Plane / ACP boundary.
    Loads profile via DB loader with safe fallback to DEFAULT_POLICY.
    Never leaks scores/evidence; returns minimal 7-key policy.
    Redis cache aware: checks cache by profile_version if available.
    """
    task_type = validate_task_type(task_type)
    cur = current_instruction or {}
    # Try Redis cache if profile_version known
    try:
        from security.models.db import get_sessionmaker as _gsm
        from security.models.orm import UserProfileORM
        import asyncio

        async def _get_ver():
            try:
                maker = _gsm()
                async with maker() as s:
                    p = await s.get(UserProfileORM, {"user_id": user_id, "tenant_id": tenant_id})
                    return p.profile_version if p else 0
            except Exception:
                return None

        # only attempt sync cache if no running loop
        try:
            asyncio.get_running_loop()
            ver = None
        except RuntimeError:
            ver = asyncio.run(_get_ver())
        if ver is not None:
            try:
                from .cache import get_cached_policy as _gcp
                cached = _gcp(tenant_id, user_id, task_type, ver)
                if cached and "policy" in cached:
                    # merge current_instruction precedence over cached explicit-derived policy
                    pol = dict(cached["policy"])
                    for k in pol:
                        if k in cur:
                            pol[k] = cur[k]
                    # ensure minimal keys
                    allowed = set(DEFAULT_POLICY.keys())
                    return {k: v for k, v in pol.items() if k in allowed}
            except Exception:
                pass
    except Exception:
        pass
    try:
        data = _sync_profile_loader(tenant_id, user_id, task_type)
        policy = default_hook.before_llm_call(tenant_id, user_id, task_type, current_instruction=cur, profile_loader=lambda tid, uid, tt: data)
        # populate cache best-effort (need version)
        try:
            import asyncio as _aio

            async def _ver_and_set():
                try:
                    from security.models.db import get_sessionmaker as _g2
                    from security.models.orm import UserProfileORM as _UP
                    maker2 = _g2()
                    async with maker2() as s2:
                        p2 = await s2.get(_UP, {"user_id": user_id, "tenant_id": tenant_id})
                        ver2 = p2.profile_version if p2 else 0
                        from .cache import set_cached_policy as _scp
                        _scp(tenant_id, user_id, task_type, ver2, policy)
                except Exception:
                    pass

            try:
                _aio.get_running_loop()
            except RuntimeError:
                _aio.run(_ver_and_set())
        except Exception:
            pass
        return policy
    except Exception as e:
        logger.warning(f"get_response_policy fallback: {e}")
        merged = dict(DEFAULT_POLICY)
        for k in merged:
            if k in cur:
                merged[k] = cur[k]
        return merged


async def get_response_policy_async(
    tenant_id: str,
    user_id: str,
    task_type: str | None = None,
    current_instruction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Async variant for ACP adapter streaming path. Redis cache aware."""
    task_type = validate_task_type(task_type)
    cur = current_instruction or {}
    # cache check (async)
    try:
        from security.models.db import get_sessionmaker as _gsm2
        from security.models.orm import UserProfileORM as _UP2
        maker = _gsm2()
        async with maker() as session:
            prof = await session.get(_UP2, {"user_id": user_id, "tenant_id": tenant_id})
            ver = prof.profile_version if prof else 0
        from .cache import get_cached_policy as _gcp2
        cached = _gcp2(tenant_id, user_id, task_type, ver)
        if cached and "policy" in cached:
            pol = dict(cached["policy"])
            for k in pol:
                if k in cur:
                    pol[k] = cur[k]
            allowed = set(DEFAULT_POLICY.keys())
            return {k: v for k, v in pol.items() if k in allowed}
    except Exception:
        pass
    try:
        data = await _async_profile_loader(tenant_id, user_id, task_type)
        policy = default_hook.before_llm_call(tenant_id, user_id, task_type, current_instruction=cur, profile_loader=lambda tid, uid, tt: data)
        # set cache
        try:
            from security.models.db import get_sessionmaker as _gsm3
            from security.models.orm import UserProfileORM as _UP3
            maker3 = _gsm3()
            async with maker3() as s3:
                prof3 = await s3.get(_UP3, {"user_id": user_id, "tenant_id": tenant_id})
                ver3 = prof3.profile_version if prof3 else 0
            from .cache import set_cached_policy as _scp3
            _scp3(tenant_id, user_id, task_type, ver3, policy)
        except Exception:
            pass
        return policy
    except Exception as e:
        logger.warning(f"get_response_policy_async fallback: {e}")
        merged = dict(DEFAULT_POLICY)
        for k in merged:
            if k in cur:
                merged[k] = cur[k]
        return merged


# Alias for compatibility
resolve_policy = get_response_policy
resolve_policy_async = get_response_policy_async

# Singleton for convenience
default_hook = AdaptiveProfileHook()
