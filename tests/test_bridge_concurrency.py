"""Regression tests for bounded concurrent Mattermost channel polling."""
import importlib.util
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "oaos-mm-bridge.py"
spec = importlib.util.spec_from_file_location("oaos_mm_bridge_concurrency", SCRIPT)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
assert spec.loader is not None
spec.loader.exec_module(bridge)


def test_channel_posts_are_fetched_concurrently_with_bound(monkeypatch):
    calls = []

    def fake_api_get(path):
        calls.append(path)
        if "/channels/" in path and "/posts" in path:
            time.sleep(0.15)
        return {"order": [], "posts": {}}

    monkeypatch.setattr(bridge, "api_get", fake_api_get)
    channels = [{"id": "channel-a", "type": "D"}, {"id": "channel-b", "type": "D"}]

    started = time.monotonic()
    result = bridge.fetch_channel_posts_parallel(channels, max_workers=2)
    elapsed = time.monotonic() - started

    assert set(result) == {"channel-a", "channel-b"}
    assert elapsed < 0.27, f"channel fetches remained sequential: {elapsed:.3f}s"
    assert len([path for path in calls if "/posts" in path]) == 2


def test_channel_fetch_worker_count_is_bounded(monkeypatch):
    observed = []

    def fake_api_get(path):
        if "/channels/" in path and "/posts" in path:
            observed.append(path)
        return {"order": [], "posts": {}}

    monkeypatch.setattr(bridge, "api_get", fake_api_get)
    channels = [{"id": f"channel-{i}", "type": "D"} for i in range(12)]
    result = bridge.fetch_channel_posts_parallel(channels, max_workers=3)

    assert len(result) == 12
    assert len(observed) == 12
    assert bridge.POLL_CHANNEL_WORKERS == 4
