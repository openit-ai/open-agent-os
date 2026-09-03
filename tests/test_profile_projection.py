from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control-plane"))
from control_plane.adaptive_profile.projection import project_behavior


def test_projection_requires_evidence_and_is_non_diagnostic():
    result = project_behavior({"planning_orientation": {"score": 1, "confidence": .9, "sample_count": 3}})
    assert result["status"] == "insufficient_evidence"
    assert result["mbti_like"]["axes"]["J_P"]["label"] == "uncertain"
    assert "진단이 아닙니다" in result["disclaimer"]


def test_projection_returns_reference_axes_after_threshold():
    traits = {
        name: {"score": .8, "confidence": .9, "sample_count": 5}
        for name in ("planning_orientation", "completion_orientation", "novelty_preference", "evidence_requirement", "critical_challenge")
    }
    result = project_behavior(traits)
    assert result["status"] == "ready"
    assert result["projection_version"] == "1.0"
    assert result["mbti_like"]["axes"]["J_P"]["label"] == "J-like"
    assert result["enneagram_like"]
