"""Regression tests for OAOS/Hermes same-OS-account file boundaries."""
from __future__ import annotations

import os
from pathlib import Path


def test_acp_adapter_has_no_hermes_global_file_fallback() -> None:
    source = Path("control-plane/control_plane/acp_adapter.py").read_text(encoding="utf-8")
    assert "Path.home() / \".hermes\"" not in source
    assert "~/.hermes/.env" not in source


def test_acp_key_resolution_uses_explicit_oaos_environment(monkeypatch) -> None:
    monkeypatch.setenv("OAOS_CP_HERMES_API_KEY", "oaos-key")
    monkeypatch.setenv("API_SERVER_KEY", "global-key")
    import sys
    sys.path.insert(0, "control-plane")
    from control_plane.acp_adapter import ACPAdapter

    adapter = ACPAdapter("http://127.0.0.1:8642")
    assert adapter._hermes_api_key() == "oaos-key"


def test_oaos_does_not_use_hermes_global_key_when_explicit_key_absent(monkeypatch) -> None:
    monkeypatch.delenv("OAOS_CP_HERMES_API_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    # Settings may cache a key from an earlier test; explicitly clear it.
    import control_plane.config as config
    monkeypatch.setattr(config.settings, "hermes_api_key", "", raising=False)
    import sys
    sys.path.insert(0, "control-plane")
    from control_plane.acp_adapter import ACPAdapter

    adapter = ACPAdapter("http://127.0.0.1:8642")
    assert adapter._hermes_api_key() == ""
