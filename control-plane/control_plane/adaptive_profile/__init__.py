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
from .hook import AdaptiveProfileHook, default_hook

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
]
