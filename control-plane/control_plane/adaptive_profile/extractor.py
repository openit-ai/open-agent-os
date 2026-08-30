"""Deterministic rule-based evidence extractor.

No LLM, no network. Maps explicit feedback phrases (Korean/English) to trait/direction.
Idempotent and deterministic — same input always yields same output.
"""
from __future__ import annotations
import re
from typing import Any

# Each rule: (regex pattern, trait, direction, strength, confidence)
# source_type fixed to explicit_feedback for all explicit phrases.
_RULES: list[tuple[re.Pattern, str, int, float, float]] = []

def _c(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)

# Verbosity: too long -> low verbosity (shorter)
_RULES.append((_c(r"(too\s+long|too\s+verbose|too\s+wordy|make\s+it\s+shorter|shorten|concise|briefly|간결하게|짧게|너무\s*길어|너무\s*길다|너무\s*장황)"), "verbosity", -1, 0.90, 0.95))
# Verbosity opposite: too short -> high verbosity (longer) - optional
_RULES.append((_c(r"(too\s+short|more\s+detail|elaborate|자세하게|더\s*길게)"), "verbosity", 1, 0.85, 0.90))

# conclusion_first
_RULES.append((_c(r"(conclusion\s*first|conclusion\s*up\s*front|start\s+with\s+conclusion|결론부터|결론\s*먼저|결론을\s*먼저)"), "conclusion_first", 1, 0.90, 0.95))

# evidence_requirement: verify sources
_RULES.append((_c(r"(verify\s+sources?|cite\s+sources?|source\s+verification|show\s+evidence|출처\s*검증|출처를\s*검증|근거\s*검증|증거\s*제시|근거를\s*제시)"), "evidence_requirement", 1, 0.90, 0.95))

# confirmation_requirement: proceed without asking -> low confirmation (high autonomy)
# maps to confirmation_requirement trait direction -1 (want low confirmation)
_RULES.append((_c(r"(proceed\s+without\s+asking|without\s+asking|don\'t\s+ask|do\s+not\s+ask|no\s+need\s+to\s+ask|묻지\s*말고|물어보지\s*말고|확인\s*없이\s*진행|묻지말고\s*진행)"), "confirmation_requirement", -1, 0.88, 0.95))
# also agent_autonomy positive when same phrase
_RULES.append((_c(r"(proceed\s+without\s+asking|묻지\s*말고\s*진행|확인\s*없이)"), "agent_autonomy", 1, 0.80, 0.90))

# critical_challenge: challenge assumptions
_RULES.append((_c(r"(challenge\s+assumptions?|question\s+assumptions?|critically?\s+challenge|비판적으로|가정을\s*반박|가정\s*비판|반박해줘)"), "critical_challenge", 1, 0.90, 0.95))


def extract_evidence(text: str, task_type: str | None = None) -> list[dict[str, Any]]:
    """Deterministic extraction. Returns list of evidence dicts sorted by trait."""
    if not text or not isinstance(text, str):
        return []
    # normalize whitespace but keep original for matching
    # use lowercased matching via regex IGNORECASE already
    seen_traits: set[str] = set()
    out: list[dict[str, Any]] = []
    for pat, trait, direction, strength, confidence in _RULES:
        if pat.search(text):
            # deduplicate same trait from multiple overlapping patterns (first wins for that trait+direction)
            key = f"{trait}:{direction}"
            if key in seen_traits:
                continue
            seen_traits.add(key)
            out.append({
                "trait": trait,
                "direction": direction,
                "strength": strength,
                "confidence": confidence,
                "source_type": "explicit_feedback",
                "task_type": task_type,
                "matched": pat.pattern[:40],
            })
    # deterministic order by trait name
    out.sort(key=lambda x: (x["trait"], x["direction"]))
    return out
