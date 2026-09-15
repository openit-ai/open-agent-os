"""Phase 2A setup-progress automatic completion contract tests."""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
spec = importlib.util.spec_from_file_location("phase2a_progress_contract_mod", BACKEND / "readiness.py")
readiness = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules[spec.name] = readiness  # type: ignore[union-attr]
spec.loader.exec_module(readiness)  # type: ignore[union-attr]


def _connection(connection_id: str) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "id": connection_id,
        "kind": connection_id,
        "state": "healthy",
        "applied": True,
        "requires_restart": False,
        "checked_at": now,
        "config_revision": f"cfg_{connection_id}",
        "effective_revision": f"cfg_{connection_id}",
        "source": "contract-test",
        "_required": True,
        "_configured": True,
        "_code": "OK",
    }


def _complete_snapshot() -> dict:
    checked_at = datetime.now(UTC).isoformat()
    return {
        "checked_at": checked_at,
        "environment": {
            "backend": {"ok": True, "code": "OK", "checked_at": checked_at},
            "database": {"ok": True, "code": "OK", "checked_at": checked_at},
            "control_plane": {"ok": True, "code": "OK", "checked_at": checked_at},
        },
        "required_connections": [
            _connection("control-plane"),
            _connection("execution"),
            _connection("ingress"),
            _connection("policy"),
        ],
        "supporting_connections": [],
        "policy_checks": {"l5_admin": True, "active_version": True, "valid": True},
        "optionals": {
            "mcp": {"selected": False, "complete": False},
            "knowledge": {"selected": False, "complete": False},
            "notifications": {"selected": False, "complete": False},
        },
    }


def test_progress_has_eight_ordered_steps_and_optional_skip():
    progress = readiness.build_setup_progress(_complete_snapshot())
    assert [step["id"] for step in progress["steps"]] == [
        "environment", "runtime", "ingress", "policy", "mcp", "knowledge",
        "notifications", "verify",
    ]
    assert [step["kind"] for step in progress["steps"]] == [
        "required", "required", "required", "required", "optional", "optional",
        "optional", "required",
    ]
    assert all(step["status"] == "skipped" for step in progress["steps"][4:7])
    assert all(step["optional_skipped"] for step in progress["steps"][4:7])
    assert progress["required_complete"] is True
    assert progress["percent"] == 100
    assert progress["current_step"] == "verify"


def test_ingress_is_required_and_console_only_is_not_supported():
    snapshot = _complete_snapshot()
    ingress = next(c for c in snapshot["required_connections"] if c["id"] == "ingress")
    ingress.update({"state": "failed", "applied": False, "_configured": False, "_code": "REQUIRED_MISSING"})
    progress = readiness.build_setup_progress(snapshot)
    step = next(s for s in progress["steps"] if s["id"] == "ingress")
    assert step["kind"] == "required"
    assert step["status"] == "incomplete"
    assert step["optional_skipped"] is False
    assert progress["required_complete"] is False
    assert progress["current_step"] == "ingress"
    assert progress["percent"] < 100


def test_completed_ingress_regresses_to_needs_attention_on_partial_failure():
    snapshot = _complete_snapshot()
    ingress = next(c for c in snapshot["required_connections"] if c["id"] == "ingress")
    ingress.update({"state": "failed", "_configured": True, "_code": "UNREACHABLE"})
    progress = readiness.build_setup_progress(snapshot)
    by_id = {step["id"]: step for step in progress["steps"]}
    assert by_id["ingress"]["status"] == "needs_attention"
    assert by_id["ingress"]["blocking_checks"][0]["code"] == "UNREACHABLE"
    assert by_id["verify"]["status"] == "needs_attention"
    assert progress["required_complete"] is False


def test_percent_counts_required_checks_not_optional_steps():
    snapshot = _complete_snapshot()
    baseline = readiness.build_setup_progress(snapshot)["percent"]
    snapshot["optionals"]["mcp"] = {"selected": True, "complete": False}
    snapshot["optionals"]["knowledge"] = {"selected": True, "complete": False}
    assert readiness.build_setup_progress(snapshot)["percent"] == baseline
