"""Adaptive Profile Runtime Hook — minimal Response Policy with Redis cache."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from .engine import DEFAULT_POLICY, TASK_TYPES, synthesize_policy

try:
    from sqlalchemy.exc import SQLAlchemyError
except (ImportError, ModuleNotFoundError):
    class SQLAlchemyError(Exception):
        """Fallback marker when SQLAlchemy is not installed."""


logger = logging.getLogger(__name__)
_CACHE_ERRORS = (ImportError, ModuleNotFoundError, OSError, RuntimeError, TimeoutError, TypeError, ValueError)
_BACKEND_ERRORS = (SQLAlchemyError, OSError, ConnectionError, TimeoutError, RuntimeError)


class ProfileHookError(RuntimeError):
    """Base error for failures at the adaptive profile hook boundary."""

    status_code = 500


class ProfileValidationError(ProfileHookError):
    """The event, context, or persisted profile shape is invalid."""

    status_code = 422


class ProfileIdentityError(ProfileHookError):
    """The hook was called without a usable identity."""

    status_code = 401


class ProfileContextMismatchError(ProfileHookError):
    """The supplied profile context does not belong to the request context."""

    status_code = 403


class ProfileBackendUnavailableError(ProfileHookError):
    """The durable profile backend could not be reached or initialized."""

    status_code = 503


def _is_production() -> bool:
    return os.getenv("OAOS_ENV", "").strip().lower() in {"production", "prod"}


def _allow_nonprod_fallback() -> bool:
    """Allow fallback only for explicit non-production/test execution."""
    if _is_production():
        return False
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    for key in ("OAOS_ALLOW_TEST_FIXTURE", "OAOS_ALLOW_TEST_FALLBACK", "OAOS_ALLOW_PROFILE_FALLBACK"):
        if os.getenv(key, "").strip().lower() in {"1", "true", "yes"}:
            return True
    return False


def _error_type(exc: BaseException) -> str:
    """Return a non-sensitive error label for logs and metrics."""
    return type(exc).__name__


def _default_policy(current_instruction: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_POLICY)
    for key in merged:
        if key in current_instruction:
            merged[key] = current_instruction[key]
    return merged


def _validate_context(
    tenant_id: str,
    user_id: str,
    task_type: str | None,
    current_instruction: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    if not isinstance(tenant_id, str) or not tenant_id.strip() or not isinstance(user_id, str) or not user_id.strip():
        raise ProfileIdentityError("adaptive profile identity is required")
    if current_instruction is None:
        current: dict[str, Any] = {}
    elif isinstance(current_instruction, dict):
        current = current_instruction
    else:
        raise ProfileValidationError("current instruction must be an object")

    for key, expected in (("tenant_id", tenant_id), ("user_id", user_id)):
        supplied = current.get(key)
        if supplied is not None and supplied != expected:
            raise ProfileContextMismatchError("adaptive profile context does not match the request")

    if task_type is None:
        resolved_task = "general_chat"
    elif isinstance(task_type, str) and task_type in TASK_TYPES:
        resolved_task = task_type
    else:
        raise ProfileValidationError("unsupported adaptive profile task type")
    return resolved_task, current


def _profile_payload(data: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not isinstance(data, dict):
        raise ProfileValidationError("adaptive profile payload must be an object")
    values: list[dict[str, Any]] = []
    for key in ("explicit_prefs", "explicit", "task_scores", "global_scores", "trait_scores"):
        value = data.get(key)
        if value is not None and not isinstance(value, dict):
            raise ProfileValidationError("adaptive profile payload contains an invalid section")
        values.append(value or {})
    explicit = values[0] or values[1]
    return explicit, values[2], values[3] or values[4]


def _minimal_policy(policy: Any, current_instruction: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(policy, dict):
        raise ProfileValidationError("adaptive profile policy must be an object")
    allowed = set(DEFAULT_POLICY.keys())
    minimal = {key: value for key, value in policy.items() if key in allowed}
    for key in minimal:
        if key in current_instruction:
            minimal[key] = current_instruction[key]
    return minimal


class AdaptiveProfileHook:
    """Resolve a minimal response policy at the verified LLM call boundary."""

    def before_llm_call(
        self,
        tenant_id: str,
        user_id: str,
        task_type: str | None = None,
        current_instruction: dict[str, Any] | None = None,
        profile_loader: Any | None = None,
    ) -> dict[str, Any]:
        """Return a seven-key policy without exposing profile evidence or scores."""
        resolved_task, current = _validate_context(tenant_id, user_id, task_type, current_instruction)
        if profile_loader is None:
            return _default_policy(current)

        try:
            data = profile_loader(tenant_id, user_id, resolved_task)
        except ProfileHookError:
            raise
        except _BACKEND_ERRORS as exc:
            if _allow_nonprod_fallback():
                logger.warning("adaptive profile loader degraded: %s", _error_type(exc))
                return _default_policy(current)
            raise ProfileBackendUnavailableError("adaptive profile backend unavailable") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileValidationError("adaptive profile loader returned invalid data") from exc

        explicit, task_scores, global_scores = _profile_payload(data)
        try:
            policy = synthesize_policy(
                current_instruction=current,
                explicit_prefs=explicit,
                task_scores=task_scores,
                global_scores=global_scores,
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ProfileValidationError("adaptive profile data could not be interpreted") from exc
        return _minimal_policy(policy, current)

    def format_prompt_injection(self, policy: dict[str, Any]) -> str:
        """Format minimal policy for LLM context injection (no scores)."""
        if not isinstance(policy, dict):
            raise ProfileValidationError("adaptive profile policy must be an object")
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
        if not isinstance(alt, (int, float)):
            raise ProfileValidationError("adaptive profile alternatives must be numeric")
        if alt > 1:
            lines.append(f"- 핵심 대안은 {int(alt)}개 이하로 제시")
        cl = policy.get("confirmation_level", "medium")
        if cl == "low":
            lines.append("- 불필요한 확인 질문 최소화")
        elif cl == "high":
            lines.append("- 실행 전 확인 질문 포함")
        return "\n".join(lines)


async def _load_profile_data(tenant_id: str, user_id: str, task_type: str) -> dict[str, Any]:
    try:
        from security.models.db import get_sessionmaker
        from security.models.orm import ExplicitPreferenceORM, TaskTraitScoreORM, TraitScoreORM
        from sqlalchemy import select
    except (ImportError, ModuleNotFoundError) as exc:
        raise ProfileBackendUnavailableError("adaptive profile backend unavailable") from exc

    try:
        maker = get_sessionmaker()
        async with maker() as session:
            explicit: dict[str, Any] = {}
            result = await session.execute(
                select(ExplicitPreferenceORM).where(
                    ExplicitPreferenceORM.user_id == user_id,
                    ExplicitPreferenceORM.tenant_id == tenant_id,
                )
            )
            for row in result.scalars().all():
                if row.scope == "global" or row.task_type == task_type:
                    explicit[row.key] = row.value
                value = explicit.get(row.key)
                if isinstance(value, str):
                    if value.lower() in ("true", "false"):
                        explicit[row.key] = value.lower() == "true"
                    elif value.isdigit():
                        explicit[row.key] = int(value)

            result = await session.execute(
                select(TraitScoreORM).where(
                    TraitScoreORM.user_id == user_id,
                    TraitScoreORM.tenant_id == tenant_id,
                )
            )
            global_scores = {row.trait_name: row.global_score for row in result.scalars().all()}
            result = await session.execute(
                select(TaskTraitScoreORM).where(
                    TaskTraitScoreORM.user_id == user_id,
                    TaskTraitScoreORM.tenant_id == tenant_id,
                    TaskTraitScoreORM.task_type == task_type,
                )
            )
            task_scores = {row.trait_name: row.score for row in result.scalars().all()}
            return {"explicit_prefs": explicit, "task_scores": task_scores, "global_scores": global_scores}
    except (TypeError, ValueError, KeyError) as exc:
        raise ProfileValidationError("adaptive profile data is malformed") from exc
    except _BACKEND_ERRORS as exc:
        raise ProfileBackendUnavailableError("adaptive profile backend unavailable") from exc


def _sync_profile_loader(tenant_id: str, user_id: str, task_type: str) -> dict[str, Any]:
    """Load durable profile data for synchronous callers; never hide production failures."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_load_profile_data(tenant_id, user_id, task_type))
    raise ProfileBackendUnavailableError("synchronous profile lookup is unavailable inside an active event loop")


async def _async_profile_loader(tenant_id: str, user_id: str, task_type: str) -> dict[str, Any]:
    """Load durable profile data for async callers."""
    return await _load_profile_data(tenant_id, user_id, task_type)


async def _load_profile_version(tenant_id: str, user_id: str) -> int:
    try:
        from security.models.db import get_sessionmaker
        from security.models.orm import UserProfileORM
    except (ImportError, ModuleNotFoundError) as exc:
        raise ProfileBackendUnavailableError("adaptive profile backend unavailable") from exc
    try:
        maker = get_sessionmaker()
        async with maker() as session:
            profile = await session.get(UserProfileORM, {"user_id": user_id, "tenant_id": tenant_id})
            return profile.profile_version if profile else 0
    except (TypeError, ValueError, KeyError) as exc:
        raise ProfileValidationError("adaptive profile version is malformed") from exc
    except _BACKEND_ERRORS as exc:
        raise ProfileBackendUnavailableError("adaptive profile backend unavailable") from exc


def _cache_policy(tenant_id: str, user_id: str, task_type: str, version: int, current: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from .cache import get_cached_policy
        cached = get_cached_policy(tenant_id, user_id, task_type, version)
    except _CACHE_ERRORS as exc:
        logger.warning("adaptive profile cache read degraded: %s", _error_type(exc))
        return None
    if not cached or not isinstance(cached, dict) or "policy" not in cached:
        return None
    return _minimal_policy(cached["policy"], current)


def _write_cache_sync(tenant_id: str, user_id: str, task_type: str, policy: dict[str, Any]) -> None:
    try:
        version = asyncio.run(_load_profile_version(tenant_id, user_id))
        from .cache import set_cached_policy
        set_cached_policy(tenant_id, user_id, task_type, version, policy)
    except ProfileHookError as exc:
        logger.warning("adaptive profile cache write degraded: %s", _error_type(exc))
    except _CACHE_ERRORS as exc:
        logger.warning("adaptive profile cache write degraded: %s", _error_type(exc))


async def _write_cache_async(tenant_id: str, user_id: str, task_type: str, policy: dict[str, Any]) -> None:
    try:
        version = await _load_profile_version(tenant_id, user_id)
        from .cache import set_cached_policy
        set_cached_policy(tenant_id, user_id, task_type, version, policy)
    except ProfileHookError as exc:
        logger.warning("adaptive profile cache write degraded: %s", _error_type(exc))
    except _CACHE_ERRORS as exc:
        logger.warning("adaptive profile cache write degraded: %s", _error_type(exc))


def get_response_policy(
    tenant_id: str,
    user_id: str,
    task_type: str | None = None,
    current_instruction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a minimal response policy for synchronous Control Plane callers."""
    resolved_task, current = _validate_context(tenant_id, user_id, task_type, current_instruction)

    try:
        version = asyncio.run(_load_profile_version(tenant_id, user_id))
        cached = _cache_policy(tenant_id, user_id, resolved_task, version, current)
        if cached is not None:
            return cached
    except ProfileBackendUnavailableError as exc:
        logger.info("adaptive profile cache version unavailable: %s", _error_type(exc))
    except ProfileValidationError:
        raise
    except _CACHE_ERRORS as exc:
        logger.warning("adaptive profile cache lookup degraded: %s", _error_type(exc))

    try:
        data = _sync_profile_loader(tenant_id, user_id, resolved_task)
        policy = default_hook.before_llm_call(
            tenant_id,
            user_id,
            resolved_task,
            current_instruction=current,
            profile_loader=lambda _tenant, _user, _task: data,
        )
    except ProfileBackendUnavailableError as exc:
        if _allow_nonprod_fallback():
            logger.warning("adaptive profile resolution degraded: %s", _error_type(exc))
            return _default_policy(current)
        raise
    _write_cache_sync(tenant_id, user_id, resolved_task, policy)
    return policy


async def get_response_policy_async(
    tenant_id: str,
    user_id: str,
    task_type: str | None = None,
    current_instruction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a minimal response policy for async ACP callers."""
    resolved_task, current = _validate_context(tenant_id, user_id, task_type, current_instruction)

    try:
        version = await _load_profile_version(tenant_id, user_id)
        cached = _cache_policy(tenant_id, user_id, resolved_task, version, current)
        if cached is not None:
            return cached
    except ProfileBackendUnavailableError as exc:
        logger.info("adaptive profile cache version unavailable: %s", _error_type(exc))
    except ProfileValidationError:
        raise
    except _CACHE_ERRORS as exc:
        logger.warning("adaptive profile cache lookup degraded: %s", _error_type(exc))

    try:
        data = await _async_profile_loader(tenant_id, user_id, resolved_task)
        policy = default_hook.before_llm_call(
            tenant_id,
            user_id,
            resolved_task,
            current_instruction=current,
            profile_loader=lambda _tenant, _user, _task: data,
        )
    except ProfileBackendUnavailableError as exc:
        if _allow_nonprod_fallback():
            logger.warning("adaptive profile resolution degraded: %s", _error_type(exc))
            return _default_policy(current)
        raise
    await _write_cache_async(tenant_id, user_id, resolved_task, policy)
    return policy


# Alias for compatibility
resolve_policy = get_response_policy
resolve_policy_async = get_response_policy_async

# Singleton for convenience
default_hook = AdaptiveProfileHook()
