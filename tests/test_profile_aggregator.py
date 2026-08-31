from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control-plane"))
from control_plane.adaptive_profile.features import extract_features
from control_plane.adaptive_profile.aggregator import aggregate_features


def test_aggregate_tracks_confidence_sessions_and_freshness():
    fs = []
    fs += extract_features("결론부터", "2026-08-01T00:00:00+00:00")
    fs += extract_features("결론부터", "2026-08-02T00:00:00+00:00")
    out = aggregate_features(fs, datetime(2026, 8, 3, tzinfo=timezone.utc))
    item = out["conclusion_first"]
    assert item.count == 2
    assert item.sessions == 2
    assert item.mean > 0
    assert 0 < item.freshness <= 1
