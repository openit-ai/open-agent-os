"""Deterministic behavioral features; not personality or medical inference."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

@dataclass(frozen=True)
class BehavioralFeature:
    name: str
    value: float
    source_type: str
    confidence: float
    observed_at: str


def extract_features(text: str, observed_at: str | None = None) -> list[BehavioralFeature]:
    text = (text or "").strip()
    if not text:
        return []
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    features: list[BehavioralFeature] = []
    def add(name: str, value: float, source: str = "general_expression", confidence: float = .4) -> None:
        features.append(BehavioralFeature(name, max(-1.0, min(1.0, value)), source, confidence, observed_at))
    if re.search(r"결론부터|결론.*먼저|conclusion.*first", text, re.I): add("conclusion_first", 1, "explicit_feedback", .95)
    if re.search(r"간결|짧게|너무.*길|concise|brief", text, re.I): add("verbosity", -1, "explicit_feedback", .95)
    if re.search(r"자세히|더.*길게|more detail|elaborate", text, re.I): add("verbosity", 1, "explicit_feedback", .9)
    if re.search(r"출처|근거|검증|증거|verify|cite", text, re.I): add("evidence_requirement", 1, "explicit_feedback", .9)
    if re.search(r"묻지 말고|확인 없이|without asking|don't ask", text, re.I):
        add("agent_autonomy", 1, "explicit_feedback", .9); add("confirmation_requirement", -1, "explicit_feedback", .9)
    if re.search(r"계획|단계|순서|plan|step", text, re.I): add("planning_orientation", 1)
    if re.search(r"완료|끝내|마무리|done|finish", text, re.I): add("completion_orientation", 1)
    if re.search(r"반박|가정.*검토|challenge|assumption", text, re.I): add("critical_challenge", 1, "explicit_feedback", .9)
    return features


def summarize_activity(events: list[dict[str, Any]]) -> dict[str, float]:
    """Return non-diagnostic activity metrics; volume alone is not a trait."""
    valid = [e for e in events if e.get("observed_at")]
    if not valid:
        return {"event_count": 0.0, "active_days": 0.0, "mean_text_length": 0.0}
    days = set()
    lengths = []
    for event in valid:
        try:
            dt = datetime.fromisoformat(str(event["observed_at"]).replace("Z", "+00:00"))
            days.add(dt.date().isoformat())
        except Exception:
            pass
        lengths.append(len(str(event.get("text") or "")))
    return {"event_count": float(len(valid)), "active_days": float(len(days)), "mean_text_length": sum(lengths) / len(lengths)}
