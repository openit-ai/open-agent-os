from __future__ import annotations
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control-plane"))
from control_plane.adaptive_profile.features import extract_features, summarize_activity


def test_behavioral_features_are_deterministic_and_weighted():
    result = extract_features("결론부터 말하고 출처를 검증해줘. 묻지 말고 진행해.", "2026-08-31T00:00:00+00:00")
    names = {f.name for f in result}
    assert {"conclusion_first", "evidence_requirement", "agent_autonomy", "confirmation_requirement"} <= names
    assert all(f.source_type == "explicit_feedback" for f in result)
    assert extract_features("결론부터 말하고 출처를 검증해줘.", "2026-08-31T00:00:00+00:00") == extract_features("결론부터 말하고 출처를 검증해줘.", "2026-08-31T00:00:00+00:00")


def test_activity_summary_does_not_classify_volume_as_personality():
    summary = summarize_activity([
        {"observed_at": "2026-08-30T00:00:00+00:00", "text": "one"},
        {"observed_at": "2026-08-31T00:00:00+00:00", "text": "three"},
    ])
    assert summary == {"event_count": 2.0, "active_days": 2.0, "mean_text_length": 4.0}
