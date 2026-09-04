"""Required-route aliases (``/v1/oauth/google/*``) for OAOS-owned Google OAuth.

Thin delegation over the canonical flow in ``control_plane.google_oauth``
(``/v1/google/oauth/*``): proves both route trees share one owner-bound,
single-use state store, and that responses never carry token material.
Fake vault/HTTP only — no network, no Hermes token files.
"""
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
for p in [ROOT / "control-plane", ROOT / "execution-gateway"]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from control_plane.app import app  # noqa: E402
from control_plane import google_oauth as go  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from test_cp_google_oauth import FakeVault, FakeGoogleHttp  # noqa: E402


@pytest.fixture
def oauth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/oauth/callback")
    go.reset_oauth_overrides()
    vault = FakeVault()
    go.set_vault_override(vault)
    log: dict = {}
    go.set_http_client_factory(lambda: FakeGoogleHttp(log))
    try:
        from delegation_service.service import DelegationService  # type: ignore
    except ImportError:
        from security.delegation.delegation_service.service import DelegationService  # type: ignore
    go.set_delegation_service_override(DelegationService())
    yield {"vault": vault, "log": log}
    go.reset_oauth_overrides()
    go.set_http_client_factory(None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


KIM = {"X-User-Id": "employee:kim", "X-Tenant-Id": "t1"}
LEE = {"X-User-Id": "employee:lee", "X-Tenant-Id": "t1"}


def _start(client: TestClient, headers: dict, **params) -> dict:
    r = client.get("/v1/oauth/google/start", params={"tenant_id": "t1", **params}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


class TestCompatStart:
    def test_start_shape_no_secrets(self, client: TestClient, oauth_env: dict) -> None:
        body = _start(client, KIM, email="kim@example.com")
        assert body["auth_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth")
        assert body["state"]
        assert "client_secret" not in body["auth_url"]
        raw = client.get("/v1/oauth/google/start", params={"tenant_id": "t1"}, headers=KIM).text
        assert "test-client-secret" not in raw

    def test_start_requires_auth(self, client: TestClient, oauth_env: dict) -> None:
        r = client.get("/v1/oauth/google/start", params={"tenant_id": "t1"})
        assert r.status_code == 401

    def test_start_state_owner_bound(self, client: TestClient, oauth_env: dict) -> None:
        body = _start(client, KIM)
        entry = go._memory_state_store._states[body["state"]]
        assert entry.user_id == "employee:kim"
        assert entry.agent_id == "agent:assistant:kim"
        assert entry.tenant_id == "t1"
        assert entry.code_verifier


class TestCompatCallback:
    def test_get_callback_metadata_only(self, client: TestClient, oauth_env: dict) -> None:
        state = _start(client, KIM)["state"]
        r = client.get("/v1/oauth/google/callback", params={"code": "code-1", "state": state})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["provider"] == "google" and body["status"] == "ACTIVE"
        assert "access_token" not in body and "refresh_token" not in body
        assert "accesstok_code-1" not in r.text and "refreshtok_code-1" not in r.text

    def test_post_callback_variant(self, client: TestClient, oauth_env: dict) -> None:
        state = _start(client, KIM)["state"]
        r = client.post("/v1/oauth/google/callback", json={"code": "code-2", "state": state})
        assert r.status_code == 200, r.text
        assert r.json()["provider"] == "google"

    def test_cross_tree_state_interop(self, client: TestClient, oauth_env: dict) -> None:
        """State issued by the canonical tree is consumable on the alias tree."""
        r = client.post("/v1/google/oauth/authorize", json={}, headers=KIM)
        assert r.status_code == 200, r.text
        state = r.json()["state"]
        r2 = client.get("/v1/oauth/google/callback", params={"code": "code-3", "state": state})
        assert r2.status_code == 200, r2.text

    def test_replay_and_expiry_denied(self, client: TestClient, oauth_env: dict) -> None:
        state = _start(client, KIM)["state"]
        assert client.get("/v1/oauth/google/callback", params={"code": "c", "state": state}).status_code == 200
        again = client.get("/v1/oauth/google/callback", params={"code": "c", "state": state})
        assert again.status_code == 400
        state2 = _start(client, KIM)["state"]
        go._memory_state_store._states[state2].expires_at = 1.0
        exp = client.get("/v1/oauth/google/callback", params={"code": "c", "state": state2})
        assert exp.status_code == 400


class TestCompatStatusRevoke:
    def _connect(self, client: TestClient) -> dict:
        state = _start(client, KIM)["state"]
        r = client.get("/v1/oauth/google/callback", params={"code": "code-9", "state": state})
        assert r.status_code == 200, r.text
        return r.json()

    def test_status_owner_ok_cross_user_denied(self, client: TestClient, oauth_env: dict) -> None:
        info = self._connect(client)
        ok = client.get("/v1/oauth/google/status", params={"delegation_id": info["delegation_id"]}, headers=KIM)
        assert ok.status_code == 200, ok.text
        assert ok.json()["secret_ref"]
        assert "access_token" not in ok.text
        denied = client.get("/v1/oauth/google/status", params={"delegation_id": info["delegation_id"]}, headers=LEE)
        assert denied.status_code == 403

    def test_status_unknown_delegation_404(self, client: TestClient, oauth_env: dict) -> None:
        r = client.get("/v1/oauth/google/status", params={"delegation_id": "dlg_nope"}, headers=KIM)
        assert r.status_code == 404

    def test_revoke_flow_and_cross_user_denied(self, client: TestClient, oauth_env: dict) -> None:
        info = self._connect(client)
        denied = client.post("/v1/oauth/google/revoke", json={"delegation_id": info["delegation_id"]}, headers=LEE)
        assert denied.status_code in (403, 404)
        ok = client.post("/v1/oauth/google/revoke", json={"delegation_id": info["delegation_id"]}, headers=KIM)
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "REVOKED"


class TestNoHermesDependency:
    def test_modules_never_reference_hermes_token(self) -> None:
        import control_plane.google_oauth as mod
        import control_plane.google_oauth_compat as compat
        import execution_gateway.connectors.google as conn
        import inspect as _inspect

        for m in (mod, compat, conn):
            src = _inspect.getsource(m)
            assert "google_token.json" not in src, m.__name__
            assert ".hermes" not in src, m.__name__

    def test_openapi_exposes_required_routes(self, client: TestClient) -> None:
        spec = client.get("/openapi.json").json()
        paths = spec["paths"]
        for p in (
            "/v1/oauth/google/start",
            "/v1/oauth/google/callback",
            "/v1/oauth/google/status",
            "/v1/oauth/google/revoke",
        ):
            assert p in paths, p
