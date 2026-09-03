"""Regression tests for OAOS/Hermes same-OS-account file boundaries."""
from __future__ import annotations

import sys
from pathlib import Path


def test_oaos_runtime_does_not_scan_hermes_global_files() -> None:
    root = Path(__file__).resolve().parents[1]
    source_paths = [
        root / "control-plane/control_plane/acp_adapter.py",
        root / "control-plane/control_plane/mattermost_adapter/webhook.py",
        root / "admin-console/backend/user_mappings.py",
    ]
    for path in source_paths:
        source = path.read_text(encoding="utf-8")
        assert "Path.home() / \".hermes\" / \".env\"" not in source
        assert "/root/.hermes/.env" not in source
        assert "~/.hermes/.env" not in source


def test_acp_key_resolution_uses_explicit_oaos_environment(monkeypatch) -> None:
    monkeypatch.setenv("OAOS_CP_HERMES_API_KEY", "oaos-key")
    monkeypatch.setenv("API_SERVER_KEY", "global-key")
    sys.path.insert(0, "control-plane")
    from control_plane.acp_adapter import ACPAdapter

    adapter = ACPAdapter("http://127.0.0.1:8642")
    assert adapter._hermes_api_key() == "oaos-key"


def test_oaos_does_not_use_hermes_global_key_when_explicit_key_absent(monkeypatch) -> None:
    monkeypatch.delenv("OAOS_CP_HERMES_API_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    sys.path.insert(0, "control-plane")
    from control_plane.acp_adapter import ACPAdapter

    adapter = ACPAdapter("http://127.0.0.1:8642")
    assert adapter._hermes_api_key() == ""
