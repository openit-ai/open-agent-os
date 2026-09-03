"""Daily briefing must use only the verified requester's Google token."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path.home() / ".hermes/scripts/daily_brief.py"


def _module():
    spec = importlib.util.spec_from_file_location("daily_brief_isolation", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_token_path_requires_verified_owner_and_never_global_fallback(tmp_path):
    module = _module()
    module.GWS_TOKENS_BASE = tmp_path / "google-tokens"
    (module.GWS_TOKENS_BASE / "mykim").mkdir(parents=True)
    (module.GWS_TOKENS_BASE / "mykim/google_token.json").write_text("{}")

    assert module._get_token_path(["mykim"], "mykim").name == "google_token.json"
    try:
        module._get_token_path(["unknown"], "unknown")
    except RuntimeError as exc:
        assert "전용 토큰" in str(exc)
    else:
        raise AssertionError("missing owner token must fail closed")


def test_canonical_user_id_uses_verified_mattermost_mapping(tmp_path):
    module = _module()
    module.USER_CHANNEL_MAP = tmp_path / "user-channel-map.json"
    module.USER_CHANNEL_MAP.write_text('{"mattermost:alice": "alice-dir"}')
    user = {"email": "wrong@example.com", "mm_ids": ["alice"]}
    assert module._canonical_user_id(user) == "alice-dir"
