"""Phase 2A readiness/discovery/test-envelope contract tests."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"


def _load(name: str, filename: str):
    added = False
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
        added = True
    try:
        spec = importlib.util.spec_from_file_location(name, str(BACKEND / filename))
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules[name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    finally:
        if added:
            sys.path.remove(str(BACKEND))


# Unique names avoid replacing canonical modules owned by legacy admin tests.
readiness_mod = _load("phase2a_readiness_contract_mod", "readiness.py")
config_modules = {
    name: _load(f"phase2a_{name}_config_mod", f"{name}_config.py")
    for name in ("acp", "mattermost", "outline", "notion", "slack", "smtp")
}


def _connection(connection_id: str) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "id": connection_id, "kind": connection_id, "state": "healthy",
        "applied": True, "requires_restart": False, "checked_at": now,
        "config_revision": f"cfg_{connection_id}",
        "effective_revision": f"cfg_{connection_id}", "source": "test",
        "_required": True, "_configured": True, "_code": "OK",
    }


def _snapshot() -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "checked_at": now,
        "environment": {
            "backend": {"ok": True, "code": "OK", "checked_at": now},
            "database": {"ok": True, "code": "OK", "checked_at": now},
            "control_plane": {"ok": True, "code": "OK", "checked_at": now},
        },
        "required_connections": [
            _connection("control-plane"), _connection("execution"),
            _connection("ingress"), _connection("policy"),
        ],
        "supporting_connections": [],
        "policy_checks": {"l5_admin": True, "active_version": True, "valid": True},
        "optionals": {
            "mcp": {"selected": False, "complete": False},
            "knowledge": {"selected": False, "complete": False},
            "notifications": {"selected": False, "complete": False},
        },
    }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'phase2a.db'}")
    monkeypatch.setenv("OAOS_ENV", "test")
    for key in (
        "OAOS_DATABASE_URL", "OAOS_CP_HERMES_BASE_URL", "HERMES_BASE_URL",
        "MATTERMOST_URL", "MATTERMOST_TOKEN", "MATTERMOST_BOT_TOKEN",
        "SLACK_WEBHOOK_URL", "SLACK_INCOMING_WEBHOOK_URL", "OAOS_SLACK_WEBHOOK_URL",
        "OUTLINE_API_URL", "OUTLINE_URL", "OUTLINE_API_KEY", "NOTION_API_KEY",
        "NOTION_TOKEN", "SMTP_HOST", "SMTP_PASSWORD", "OAOS_ADMIN_DISCOVERY_MANIFEST_JSON",
    ):
        monkeypatch.delenv(key, raising=False)

    app = FastAPI()
    app.include_router(readiness_mod.router)
    for module in config_modules.values():
        app.include_router(module.router)
    dummy_admin = SimpleNamespace(email="admin@example.test", role="L5")
    dependencies = {readiness_mod.get_current_admin, readiness_mod.require_l5}
    for module in config_modules.values():
        dependencies.update({module.get_current_admin, module.require_l5})
        engine = getattr(module, "_db_engine", None)
        if engine is not None:
            try:
                engine.dispose()
            except Exception:  # noqa: BLE001,S110 - test cleanup is best-effort
                pass
            module._db_engine = None
        module._inmem = None
    for dependency in dependencies:
        app.dependency_overrides[dependency] = lambda: dummy_admin
    readiness_mod._candidate_refs.clear()
    readiness_mod._recent_tests.clear()
    with TestClient(app) as test_client:
        yield test_client


def test_readiness_contract_and_state_classification(client, monkeypatch):
    monkeypatch.setattr(readiness_mod, "_collect_snapshot", _snapshot)
    response = client.get("/v1/admin/readiness")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["overall"] == "healthy"
    assert body["required_total"] == body["required_healthy"] == 4
    assert body["problems"] == []
    assert {"control-plane", "execution", "ingress", "policy"} == {
        item["id"] for item in body["connections"]
    }
    for item in body["connections"]:
        assert item["state"] in {"healthy", "warning", "failed"}
        assert {
            "id", "kind", "state", "applied", "requires_restart", "checked_at",
            "config_revision", "effective_revision",
        }.issubset(item)

    now = datetime.now(UTC)
    healthy = {"ok": True, "status": "healthy", "code": "OK", "checked_at": now.isoformat()}
    assert readiness_mod._classify(required=True, configured=True, applied=True, observation=healthy, now=now)[0] == "healthy"
    assert readiness_mod._classify(required=True, configured=True, applied=False, observation=healthy, now=now) == ("warning", "NOT_APPLIED")
    assert readiness_mod._classify(required=True, configured=False, applied=False, observation=None, now=now)[0] == "failed"


def test_discovery_uses_only_fixed_defaults_and_redacts_secrets(client, monkeypatch):
    monkeypatch.setattr(readiness_mod, "_registry_candidates", lambda kind: [])
    monkeypatch.setattr(readiness_mod, "_saved_candidate", lambda kind: [])
    response = client.get("/v1/admin/connections/discovery?kind=control-plane")
    assert response.status_code == 200
    defaults = [c for c in response.json()["candidates"] if c["source"] == "default"]
    assert {c["display_target"] for c in defaults} == {"127.0.0.1:8100"}
    assert all(c["candidate_id"].startswith("cand_") for c in defaults)

    secret = "token-must-not-leak"
    monkeypatch.setenv("MATTERMOST_URL", f"http://mm.internal:8065/path?access_token={secret}")
    monkeypatch.setenv("MATTERMOST_TOKEN", secret)
    redacted = client.get("/v1/admin/connections/discovery?kind=mattermost")
    dumped = json.dumps(redacted.json())
    assert secret not in dumped
    assert "access_token" not in dumped
    assert any(c["display_target"] == "mm.internal:8065" for c in redacted.json()["candidates"])

    empty = client.get("/v1/admin/connections/discovery?kind=unsupported")
    assert empty.status_code == 200
    assert empty.json()["candidates"] == []
    assert empty.json()["reasons"]


@pytest.mark.parametrize(
    ("adapter_result", "adapter_error", "expected"),
    [
        ({"status_code": 200, "latency_ms": 3.5}, None, "OK"),
        ({"status_code": 401}, None, "AUTH_REQUIRED"),
        ({"status_code": 403}, None, "PERMISSION_DENIED"),
        (None, TimeoutError(), "TIMEOUT"),
        (None, ConnectionRefusedError(), "UNREACHABLE"),
        (None, RuntimeError("raw body must not escape"), "UNKNOWN"),
    ],
)
def test_standard_test_envelope_normalizes_failures(
    client, monkeypatch, adapter_result, adapter_error, expected
):
    revision = "cfg_effective"
    monkeypatch.setattr(
        readiness_mod,
        "_connection_config",
        lambda connection_id: {
            "config": {}, "source": "env", "target": "http://service.internal/path?token=secret",
            "persisted": True, "applied": True, "config_revision": revision,
            "effective_revision": revision, "requires_restart": False,
        },
    )

    def fake_adapter(*args, **kwargs):
        if adapter_error is not None:
            raise adapter_error
        return adapter_result

    monkeypatch.setattr(readiness_mod, "_run_adapter", fake_adapter)
    response = client.post(
        "/v1/admin/connections/acp/test",
        headers={"X-Request-Timeout-Ms": "750"}, json={"mode": "safe"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == expected
    assert body["status"] in {"healthy", "warning", "failed"}
    assert body["ok"] is (expected == "OK")
    assert body["correlation_id"].startswith("conn_")
    assert {
        "ok", "status", "code", "summary", "message_key", "checked_at",
        "applied", "requires_restart", "correlation_id",
    }.issubset(body)
    dumped = json.dumps(body)
    assert "raw body must not escape" not in dumped
    assert "token=secret" not in dumped


def test_revision_mismatch_is_not_applied_envelope(client, monkeypatch):
    monkeypatch.setattr(
        readiness_mod,
        "_connection_config",
        lambda connection_id: {
            "config": {}, "source": "env", "target": "service.internal",
            "persisted": True, "applied": True, "config_revision": "cfg_new",
            "effective_revision": "cfg_old", "requires_restart": False,
        },
    )
    response = client.post(
        "/v1/admin/connections/acp/test", json={"config_revision": "cfg_new"}
    )
    assert response.status_code == 200
    assert response.json()["code"] == "NOT_APPLIED"
    assert response.json()["status"] == "warning"


def test_existing_config_response_has_additive_apply_state(client):
    required = {
        "persisted", "applied", "config_revision", "effective_revision",
        "requires_restart", "apply_strategy", "updated_at",
    }
    for path in (
        "/v1/acp/config", "/v1/mattermost/config", "/v1/outline/config",
        "/v1/notion/config", "/v1/slack/config", "/v1/smtp/config",
    ):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert required.issubset(response.json()), path
