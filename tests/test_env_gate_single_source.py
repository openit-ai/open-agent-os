from __future__ import annotations

from agent_runtime import env_gate as canonical
from control_plane import env_gate as control_plane_gate
from execution_gateway import env_gate as execution_gateway_gate


def test_runtime_gates_are_single_source_exports() -> None:
    assert control_plane_gate.is_production is canonical.is_production
    assert control_plane_gate.is_mock_allowed is canonical.is_mock_allowed
    assert execution_gateway_gate.is_production is canonical.is_production
    assert execution_gateway_gate.is_mock_allowed is canonical.is_mock_allowed


def test_production_mock_is_always_denied(monkeypatch) -> None:
    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.setenv("OAOS_MOCK_FALLBACK", "1")
    assert canonical.is_mock_allowed() is False
