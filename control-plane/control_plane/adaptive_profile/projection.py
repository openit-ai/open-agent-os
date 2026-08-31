"""Versioned, non-diagnostic projections of measured behavior traits."""
from __future__ import annotations

from typing import Any

PROJECTION_VERSION = "1.0"
DISCLAIMER = "행동 관찰 기반 참고 표시이며 성격·심리 진단이 아닙니다."
_MIN_CONFIDENCE = 0.70
_MIN_SAMPLES = 3


def _axis(label_a: str, label_b: str, score: float, confidence: float, samples: int) -> dict[str, Any]:
    enough = confidence >= _MIN_CONFIDENCE and samples >= _MIN_SAMPLES
    if not enough:
        return {"label": "uncertain", "confidence": round(confidence, 3), "sample_count": samples}
    return {"label": label_a if score >= 0 else label_b, "confidence": round(confidence, 3), "sample_count": samples}


def project_behavior(traits: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return MBTI-like axes and Enneagram-like motivations, never a diagnosis."""
    def get(name: str) -> tuple[float, float, int]:
        value = traits.get(name) or {}
        return float(value.get("score", 0.0)), float(value.get("confidence", 0.0)), int(value.get("sample_count", 0))

    planning, planning_conf, planning_n = get("planning_orientation")
    completion, completion_conf, completion_n = get("completion_orientation")
    novelty, novelty_conf, novelty_n = get("novelty_preference")
    evidence, evidence_conf, evidence_n = get("evidence_requirement")
    challenge, challenge_conf, challenge_n = get("critical_challenge")
    depth, depth_conf, depth_n = get("explanation_depth")
    scores = {
        "J_P": _axis("J-like", "P-like", (planning + completion) / 2, min(planning_conf, completion_conf), min(planning_n, completion_n)),
        "S_N": _axis("S-like", "N-like", evidence - novelty, min(evidence_conf, novelty_conf), min(evidence_n, novelty_n)),
        "T_F": _axis("T-like", "F-like", challenge + evidence, min(challenge_conf, evidence_conf), min(challenge_n, evidence_n)),
        "E_I": {"label": "insufficient_evidence", "confidence": 0.0, "sample_count": 0},
    }
    motivations = []
    if planning_n >= _MIN_SAMPLES and completion_n >= _MIN_SAMPLES and min(planning_conf, completion_conf) >= _MIN_CONFIDENCE:
        motivations.append({"label": "achievement_and_completion_like", "confidence": round(min(planning_conf, completion_conf), 3)})
    if evidence_n >= _MIN_SAMPLES and challenge_n >= _MIN_SAMPLES and min(evidence_conf, challenge_conf) >= _MIN_CONFIDENCE:
        motivations.append({"label": "control_and_certainty_like", "confidence": round(min(evidence_conf, challenge_conf), 3)})
    if novelty_n >= _MIN_SAMPLES and novelty_conf >= _MIN_CONFIDENCE:
        motivations.append({"label": "novelty_and_exploration_like", "confidence": round(novelty_conf, 3)})
    usable = [v["confidence"] for v in scores.values() if v["label"] not in ("uncertain", "insufficient_evidence")]
    return {
        "projection_version": PROJECTION_VERSION,
        "status": "ready" if usable else "insufficient_evidence",
        "mbti_like": {"axes": scores},
        "enneagram_like": motivations,
        "disclaimer": DISCLAIMER,
    }
