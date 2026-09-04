"""Control-plane OAOS-owned Google OAuth tests — fake vault/httpx, no network.

Covers: state owner binding/expiry/one-time replay, profile-email mismatch
(no storage), metadata-only exchange (no tokens), status cross-owner denial,
revoke, and production fail-closed without a vault.
"""

import os
import time

import pytest
from collections.abc import Iterator
from fastapi.testclient import TestClient

from control_plane.app import app
from control_plane import google_oauth as go


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeVault:
    """Duck-typed EncryptedPostgresVault with owner isolation."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self._meta: dict[str, dict] = {}
        self._n = 0

    async def store(self, user_id: str, provider: str, scope: str, token: bytes) -> str:
        self._n += 1
        ref = f"secret_fake{self._n:04d}"
        suffix = user_id.split(":")[-1] if ":" in user_id else user_id
        self._store[ref] = token
        self._meta[ref] = {"user_id": user_id, "owner_agent_id": f"agent:assistant:{suffix}"}
        return ref

    async def retrieve(self, secret_ref: str, requester_agent_id: str) -> bytes:
        if secret_ref not in self._store:
            raise KeyError(f"secret not found: {secret_ref}")
        if self._meta[secret_ref]["owner_agent_id"] != requester_agent_id:
            raise PermissionError("credential isolation violation")
        return self._store[secret_ref]

    async def revoke(self, secret_ref: str) -> None:
        if secret_ref not in self._store:
            raise KeyError(f"secret not found: {secret_ref}")
        del self._store[secret_ref]
        del self._meta[secret_ref]

    def owner_of(self, secret_ref: str) -> str | None:
        m = self._meta.get(secret_ref)
        return m["owner_agent_id"] if m else None


class FakeResp:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeGoogleHttp:
    """Maps auth codes -> token sets, access tokens -> userinfo emails."""

    def __init__(self, log: dict) -> None:
        self._log = log

    async def __aenter__(self) -> "FakeGoogleHttp":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(self, url: str, data: dict | None = None) -> FakeResp:
        data = data or {}
        self._log.setdefault("posts", []).append({"url": url, "keys": sorted(data.keys())})
        if "revoke" in url:
            return FakeResp({}, 200)
        code = str(data.get("code", ""))
        access = f"accesstok_{code}"
        scope = " ".join(go.DEFAULT_SCOPES)
        payload: dict = {"access_token": access, "scope": scope, "expires_in": 3600, "token_type": "Bearer"}
        if code != "code-norefresh":
            payload["refresh_token"] = f"refreshtok_{code}"
        email = "lee@example.com" if code == "code-lee-mail" else "kim@example.com"
        self._log.setdefault("userinfo", {})[access] = email
        return FakeResp(payload, 200)

    async def get(self, url: str, headers: dict | None = None) -> FakeResp:
        token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        email = self._log.get("userinfo", {}).get(token, "kim@example.com")
        return FakeResp({"email": email, "email_verified": True}, 200)


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
    # Real DelegationService (memory fallback — DB env removed above).
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


def _authorize(client: TestClient, headers: dict, body: dict | None = None) -> dict:
    r = client.post("/v1/google/oauth/authorize", json=body or {}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _callback(client: TestClient, state: str, code: str):
    return client.get("/v1/google/oauth/callback", params={"state": state, "code": code})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_authorize_binds_owner_and_pkce(client: TestClient, oauth_env: dict) -> None:
    body = _authorize(client, KIM)
    assert "accounts.google.com" in body["authorization_url"]
    assert "code_challenge=" in body["authorization_url"]
    assert "code_challenge_method=S256" in body["authorization_url"]
    assert "client_secret" not in body["authorization_url"]
    assert body["state"]
    assert body["expires_in"] > 0


def test_callback_success_metadata_only_no_tokens(client: TestClient, oauth_env: dict) -> None:
    state = _authorize(client, KIM)["state"]
    r = _callback(client, state, "code-abc")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["provider"] == "google"
    assert data["delegation_id"]
    assert data["has_refresh_token"] is True
    assert data["status"] == "ACTIVE"
    text = r.text
    assert "accesstok_" not in text
    assert "refreshtok_" not in text
    assert "access_token" not in data and "refresh_token" not in data
    # Vault holds the owner-bound bundle.
    assert len(oauth_env["vault"]._store) == 1
    # Token endpoint received the secret server-side (key present, value not logged).
    posts = oauth_env["log"]["posts"]
    assert any("oauth2.googleapis.com/token" in p["url"] for p in posts)


def test_callback_replay_denied(client: TestClient, oauth_env: dict) -> None:
    state = _authorize(client, KIM)["state"]
    r1 = _callback(client, state, "code-abc")
    assert r1.status_code == 200, r1.text
    r2 = _callback(client, state, "code-abc")
    assert r2.status_code == 400
    assert "already-used" in r2.json()["detail"] or "invalid" in r2.json()["detail"]


def test_callback_expired_state_denied(client: TestClient, oauth_env: dict) -> None:
    entry = go.OAuthStateEntry(
        state="expired-state-1",
        tenant_id="t1",
        user_id="employee:kim",
        agent_id="agent:assistant:kim",
        scopes=list(go.DEFAULT_SCOPES),
        code_verifier="verifier",
        created_at=time.time() - 900,
        expires_at=time.time() - 300,
    )
    go.get_state_store().save(entry)
    r = _callback(client, "expired-state-1", "code-abc")
    assert r.status_code == 400


def test_profile_email_mismatch_stores_nothing(client: TestClient, oauth_env: dict) -> None:
    body = _authorize(client, KIM, {"expected_email": "kim@example.com"})
    r = _callback(client, body["state"], "code-lee-mail")
    assert r.status_code == 403, r.text
    assert "PROFILE_EMAIL_MISMATCH" in r.json()["detail"]
    assert oauth_env["vault"]._store == {}


def test_status_cross_owner_denied_and_owner_ok(client: TestClient, oauth_env: dict) -> None:
    state = _authorize(client, KIM)["state"]
    dlg = _callback(client, state, "code-abc").json()["delegation_id"]
    r_denied = client.get("/v1/google/oauth/status", params={"delegation_id": dlg}, headers=LEE)
    assert r_denied.status_code == 403
    r_ok = client.get("/v1/google/oauth/status", params={"delegation_id": dlg}, headers=KIM)
    assert r_ok.status_code == 200, r_ok.text
    data = r_ok.json()
    assert data["secret_ref"]
    assert "accesstok_" not in r_ok.text


def test_revoke_flow(client: TestClient, oauth_env: dict) -> None:
    state = _authorize(client, KIM)["state"]
    dlg = _callback(client, state, "code-abc").json()["delegation_id"]
    r = client.post("/v1/google/oauth/revoke", json={"delegation_id": dlg}, headers=KIM)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REVOKED"
    assert r.json()["google_revoked"] is True
    assert "accesstok_" not in r.text
    st = client.get("/v1/google/oauth/status", params={"delegation_id": dlg}, headers=KIM)
    assert st.status_code == 200
    assert st.json()["status"] == "REVOKED"


def test_revoke_cross_owner_denied(client: TestClient, oauth_env: dict) -> None:
    state = _authorize(client, KIM)["state"]
    dlg = _callback(client, state, "code-abc").json()["delegation_id"]
    r = client.post("/v1/google/oauth/revoke", json={"delegation_id": dlg}, headers=LEE)
    assert r.status_code == 403


def test_production_vault_missing_fail_closed(client: TestClient, oauth_env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from control_plane.auth import issue_user_jwt

    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.setattr(go, "get_vault", lambda: None)
    token = issue_user_jwt("employee:kim", "t1")
    headers = dict(KIM)
    headers["Authorization"] = f"Bearer {token}"
    r = client.post("/v1/google/oauth/authorize", json={}, headers=headers)
    assert r.status_code == 503
    assert "vault" in r.json()["detail"].lower()


def test_authorize_with_session_binding(client: TestClient, oauth_env: dict) -> None:
    s = client.post("/v1/sessions", json={"tenant_id": "t1", "user_id": "employee:kim"}, headers=KIM)
    assert s.status_code == 200, s.text
    sid = s.json()["session_id"]
    ok = _authorize(client, KIM, {"session_id": sid})
    assert ok["state"]
    # Another user's session must not bind.
    r = client.post("/v1/google/oauth/authorize", json={"session_id": sid}, headers=LEE)
    assert r.status_code in (403, 404)


def test_callback_requires_state_and_code(client: TestClient, oauth_env: dict) -> None:
    assert client.get("/v1/google/oauth/callback", params={"state": "x"}).status_code == 400
    assert client.get("/v1/google/oauth/callback", params={"code": "y"}).status_code == 400
