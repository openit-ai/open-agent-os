"""Phase 2A setup-progress automatic completion contract tests."""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

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


def test_setup_next_actions_use_the_new_canonical_ia():
    assert readiness._NEXT_ACTIONS["execution"]["href"] == "/connections/harness/llm-runtime"
    assert readiness._NEXT_ACTIONS["mcp"]["href"] == "/control/mcp"
    assert readiness._NEXT_ACTIONS["knowledge"]["href"] == "/connections/knowledge/outline"
    legacy_prefixes = ("/execution/", "/knowledge/", "/operations/services")
    assert all(
        not action["href"].startswith(legacy_prefixes)
        for action in readiness._NEXT_ACTIONS.values()
    )


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


def test_apply_state_uses_config_and_effective_revision_equality():
    desired = {"enabled": True, "url": "http://service.internal"}

    env_only = readiness.apply_state_fields("sample", desired, "env")
    db_applied = readiness.apply_state_fields(
        "sample", desired, "db", effective_config=dict(desired),
    )
    db_pending = readiness.apply_state_fields(
        "sample", desired, "db",
        effective_config={"enabled": False, "url": "http://service.internal"},
    )

    assert env_only["applied"] is True
    assert env_only["requires_restart"] is False
    assert env_only["config_revision"] == env_only["effective_revision"]
    assert db_applied["applied"] is True
    assert db_applied["requires_restart"] is False
    assert db_applied["apply_strategy"] == "immediate"
    assert db_pending["applied"] is False
    assert db_pending["requires_restart"] is True
    assert db_pending["apply_strategy"] == "restart_required"


def test_matching_db_and_env_config_complete_runtime_and_ingress(monkeypatch):
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    acp_config = {
        "acp_enabled": True, "hermes_base_url": "http://hermes.internal:8642",
        "hermes_model": "default", "api_key_set": True,
    }
    mattermost_config = {
        "mattermost_url": "http://mattermost.internal:8065", "bot_token_set": True,
        "bot_username": "oaos", "default_display_name": "",
    }
    slack_config = {"webhook_url_set": False}
    modules = {
        "runtime_mode": SimpleNamespace(get_mode=lambda: SimpleNamespace(value="hermes")),
        "acp_config": SimpleNamespace(
            _load_config=lambda: (dict(acp_config), "db"),
            _env_config=lambda: dict(acp_config),
        ),
        "mattermost_config": SimpleNamespace(
            _load_config=lambda: (dict(mattermost_config), "db"),
            _env_config=lambda: dict(mattermost_config),
        ),
        "slack_config": SimpleNamespace(
            _load_config=lambda: (dict(slack_config), "env"),
            _env_config=lambda: dict(slack_config),
        ),
    }
    observations = {
        name: {
            "ok": True, "status": "healthy", "code": "OK",
            "checked_at": now.isoformat(), "last_success_at": now.isoformat(),
        }
        for name in ("acp", "mattermost")
    }
    monkeypatch.setattr(readiness, "_domain", lambda name: modules[name])
    monkeypatch.setattr(readiness, "_redacted_observation", observations.get)

    runtime, _ = readiness._runtime_connection(now)
    ingress, _ = readiness._ingress_connection(now)

    assert (runtime["state"], runtime["applied"], runtime["requires_restart"]) == (
        "healthy", True, False,
    )
    assert (ingress["state"], ingress["applied"], ingress["requires_restart"]) == (
        "healthy", True, False,
    )

    fixed_snapshot = _complete_snapshot()
    fixed_snapshot["required_connections"][1] = runtime
    fixed_snapshot["required_connections"][2] = ingress
    assert readiness.build_setup_progress(fixed_snapshot)["percent"] == 100

    stale_snapshot = _complete_snapshot()
    for connection_id in ("execution", "ingress"):
        connection = next(
            item for item in stale_snapshot["required_connections"]
            if item["id"] == connection_id
        )
        connection.update({"state": "warning", "applied": False, "_code": "NOT_APPLIED"})
    stale_progress = readiness.build_setup_progress(stale_snapshot)
    assert stale_progress["percent"] == 64
    assert readiness.build_setup_progress(fixed_snapshot)["percent"] == 100
