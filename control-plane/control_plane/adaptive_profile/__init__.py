"""Adaptive Profile — package public surface."""

from .engine import (
    TRAITS,
    TASK_TYPES,
    EVIDENCE_WEIGHTS,
    DEFAULT_POLICY,
    weighted_update,
    compute_confidence,
    synthesize_policy,
    content_hash,
    validate_trait,
    validate_task_type,
)
from .hook import AdaptiveProfileHook, default_hook, get_response_policy, get_response_policy_async, resolve_policy, resolve_policy_async
from .extractor import extract_evidence
from .worker import handle_interaction_event, handle_interaction_event_async, get_sessionmaker as worker_get_sessionmaker
from .cache import get_cached_policy, set_cached_policy, invalidate_user_cache, set_cache_client, clear_cache_client, cache_key_for_test
from .skills import register_profile_skills

__all__ = [
    "TRAITS",
    "TASK_TYPES",
    "EVIDENCE_WEIGHTS",
    "DEFAULT_POLICY",
    "weighted_update",
    "compute_confidence",
    "synthesize_policy",
    "content_hash",
    "validate_trait",
    "validate_task_type",
    "AdaptiveProfileHook",
    "default_hook",
    "get_response_policy",
    "get_response_policy_async",
    "resolve_policy",
    "resolve_policy_async",
    "extract_evidence",
    "handle_interaction_event",
    "handle_interaction_event_async",
    "get_cached_policy",
    "set_cached_policy",
    "invalidate_user_cache",
    "set_cache_client",
    "clear_cache_client",
    "cache_key_for_test",
    "register_profile_skills",
]
