"""Adaptive Profile Engine — deterministic weighted evidence + precedence."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

# ── Trait taxonomy (v1.7.2 §16.12.5) ──────────────────────────────────────
TRAITS: list[str] = [
    "conclusion_first",
    "verbosity",
    "directness",
    "explanation_depth",
    "repetition_tolerance",
    "evidence_requirement",
    "quantitative_preference",
    "critical_challenge",
    "uncertainty_tolerance",
    "recommendation_decisiveness",
    "alternative_preference",
    "risk_tolerance",
    "novelty_preference",
    "decision_speed",
    "agent_autonomy",
    "confirmation_requirement",
    "planning_orientation",
    "completion_orientation",
    "experimentation_preference",
    "delegation_preference",
    "control_preference",
    "disagreement_tolerance",
]

TASK_TYPES: list[str] = [
    "general_chat",
    "technical_research",
    "software_engineering",
    "architecture",
    "decision_support",
    "writing",
    "meeting",
    "calendar",
    "email",
    "project_management",
    "data_analysis",
    "brainstorming",
    "strategy",
]

# Evidence source weights (doc §4.3 / §16.12.3)
EVIDENCE_WEIGHTS: dict[str, float] = {
    "explicit_feedback": 1.00,
    "repeated_correction": 0.90,
    "actual_choice": 0.85,
    "work_pattern": 0.70,
    "general_expression": 0.40,
    "style_inference": 0.25,
}

# Fallback alias mapping (user-facing names)
EVIDENCE_TYPE_ALIASES = {
    "explicit_instruction": "explicit_feedback",
    "repeated_modification": "repeated_correction",
    "choice": "actual_choice",
    "pattern": "work_pattern",
    "expression": "general_expression",
    "style": "style_inference",
}

# Valid source types set
VALID_SOURCE_TYPES = set(EVIDENCE_WEIGHTS.keys())

# Default policy when no profile / hook fallback
DEFAULT_POLICY: dict[str, Any] = {
    "conclusion_first": False,
    "verbosity": "medium",
    "technical_depth": "medium",
    "evidence_requirement": "medium",
    "challenge_assumptions": False,
    "alternatives": 1,
    "confirmation_level": "medium",
}

# Learning rate for deterministic EMA
LR = 0.18
DECAY_TERM = 0.02  # slight pull toward 0 for stability


def normalize_source_type(s: str) -> str:
    if s in EVIDENCE_WEIGHTS:
        return s
    if s in EVIDENCE_TYPE_ALIASES:
        return EVIDENCE_TYPE_ALIASES[s]
    return s  # let caller validate


def validate_trait(trait: str) -> None:
    if trait not in TRAITS:
        raise ValueError(f"unknown trait: {trait}")


def validate_task_type(task_type: str | None) -> str:
    if not task_type or task_type not in TASK_TYPES:
        return "general_chat"
    return task_type


def content_hash(tenant_id: str, user_id: str, trait: str, direction: int, source_type: str, observed_at: str, extra: str = "") -> str:
    raw = f"{tenant_id}|{user_id}|{trait}|{direction}|{source_type}|{observed_at}|{extra}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def clamp_score(v: float) -> float:
    return max(-1.0, min(1.0, v))


def weighted_update(old_score: float, direction: int, strength: float, source_type: str, confidence: float) -> float:
    """Deterministic weighted EMA. direction is -1 or 1."""
    w = EVIDENCE_WEIGHTS.get(normalize_source_type(source_type), 0.40)
    # contribution in [-1,1] range weighted
    contrib = direction * max(0.0, min(1.0, strength)) * max(0.0, min(1.0, confidence)) * w
    # EMA: new = old*(1 - LR*abs(contrib_factor)) + contrib*LR
    # Use decay toward 0
    lr_eff = LR * (0.5 + 0.5 * abs(contrib))
    decayed = old_score * (1 - DECAY_TERM)
    new = decayed * (1 - lr_eff) + contrib * lr_eff
    return clamp_score(new)


def compute_confidence(sample_count: int) -> float:
    """Deterministic confidence 0..0.99 based on sample count."""
    # 0->0.3, 1->0.45, 3->0.65, 10->0.92
    if sample_count <= 0:
        return 0.30
    # logistic-like
    return min(0.99, 0.30 + 0.35 * (1 - pow(0.65, sample_count)) + min(0.30, sample_count * 0.03))


def score_to_level(score: float, low_thresh: float = -0.33, high_thresh: float = 0.33) -> str:
    if score <= low_thresh:
        return "low"
    if score >= high_thresh:
        return "high"
    return "medium"


def synthesize_policy(
    current_instruction: dict[str, Any] | None = None,
    explicit_prefs: dict[str, Any] | None = None,
    task_scores: dict[str, float] | None = None,
    global_scores: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Precedence: current_instruction > explicit_preference > task_preference > behavioral_profile > default.
    Input score dicts map trait_name -> float in [-1,1]. Explicit/current are already resolved policy values.
    Returns minimal Response Policy (7 keys, no scores/evidence).
    """
    current_instruction = current_instruction or {}
    explicit_prefs = explicit_prefs or {}
    task_scores = task_scores or {}
    global_scores = global_scores or {}

    def resolve_trait(trait: str, default: float = 0.0) -> float:
        # precedence among scored layers: explicit prefs that map to trait override scores
        # For scored traits, task > global
        if trait in task_scores:
            return task_scores[trait]
        if trait in global_scores:
            return global_scores[trait]
        return default

    # Determine each policy field with full precedence
    # Helper to check if any of the precedence layers has explicit value
    def policy_value(key: str, trait_for_score: str, mapping_fn):
        if key in current_instruction and current_instruction[key] is not None:
            return current_instruction[key]
        if key in explicit_prefs and explicit_prefs[key] is not None:
            return explicit_prefs[key]
        # task/global score derived
        sc = None
        if trait_for_score in task_scores:
            sc = task_scores[trait_for_score]
        elif trait_for_score in global_scores:
            sc = global_scores[trait_for_score]
        if sc is not None:
            return mapping_fn(sc)
        return mapping_fn(0.0)

    # conclusion_first: bool, trait conclusion_first
    def map_conclusion(v: float) -> bool:
        return v > 0.15

    def map_verbosity(v: float) -> str:
        return score_to_level(v, -0.3, 0.3)

    def map_depth(v: float) -> str:
        return score_to_level(v, -0.3, 0.3)

    def map_evidence(v: float) -> str:
        return score_to_level(v, -0.3, 0.3)

    def map_challenge(v: float) -> bool:
        return v > 0.2

    def map_alternatives(v: float) -> int:
        # alternative_preference -1 => 1, 0=>2, 1=>3 capped
        if v < -0.3:
            return 1
        if v < 0.3:
            return 2
        return 3

    def map_confirmation(v: float) -> str:
        # confirmation_requirement high => confirmation_level high
        # invert autonomy? but keep direct
        if v < -0.3:
            return "low"
        if v > 0.3:
            return "high"
        return "medium"

    policy: dict[str, Any] = {}
    policy["conclusion_first"] = policy_value("conclusion_first", "conclusion_first", map_conclusion)
    policy["verbosity"] = policy_value("verbosity", "verbosity", map_verbosity)
    policy["technical_depth"] = policy_value("technical_depth", "explanation_depth", map_depth)
    policy["evidence_requirement"] = policy_value("evidence_requirement", "evidence_requirement", map_evidence)
    policy["challenge_assumptions"] = policy_value("challenge_assumptions", "critical_challenge", map_challenge)
    policy["alternatives"] = policy_value("alternatives", "alternative_preference", map_alternatives)
    policy["confirmation_level"] = policy_value("confirmation_level", "confirmation_requirement", map_confirmation)

    # Clamp types: ensure minimal policy never leaks scores
    # Convert any explicit score-like floats that slipped through
    return policy


def validate_evidence_payload(d: dict) -> None:
    if d.get("direction") not in (-1, 1):
        raise ValueError("direction must be -1 or 1")
    for k in ("strength", "confidence"):
        v = d.get(k)
        if v is None or not (0.0 <= float(v) <= 1.0):
            raise ValueError(f"{k} must be in [0,1]")
    if d.get("source_type") not in VALID_SOURCE_TYPES and d.get("source_type") not in EVIDENCE_TYPE_ALIASES:
        raise ValueError(f"unknown source_type: {d.get('source_type')}")
    validate_trait(d.get("trait", ""))
    # task_type: allow unknown -> general_chat (fail-safe), but validate for storage
    # we normalize later
