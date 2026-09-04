"""GoogleConnector OAuth-owned credential resolver tests (local only).

Fake Vault + mocked httpx — no real keys, no network, no Hermes files.
Covers: binding/provider resolution, Vault retrieve, expiry refresh via the
Google token endpoint (client env), Vault-only persistence of refreshed
bundles, read-only Bearer API path, production fail-closed vs dev skeleton.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in [
    ROOT / "execution-gateway",
    ROOT / "security/credential-vault",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from execution_gateway.connectors.google import (  # type: ignore
    GOOGLE_TOKEN_URL,
    READONLY_TOOLS,
    GoogleConnector,
    _encode_token_bundle,
)


# ---------------------------------------------------------------------------
# fakes (no real secrets — all values are synthetic test fixtures)
# ---------------------------------------------------------------------------
class FakeVault:
    """Minimal Vault double: owner-bound retrieve, ref-issuing store."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._meta: dict[str, dict] = {}
        self._counter = 0
        self.store_calls: list[dict] = []
        self.retrieve_calls: list[dict] = []
        self.revoked: list[str] = []

    @staticmethod
    def _owner(user_id: str) -> str:
        return f"agent:assistant:{user_id.split(':', 1)[-1]}"

    async def store(self, user_id: str, provider: str, scope: str, token: bytes) -> str:
        self._counter += 1
        ref = f"secret_fake{self._counter:04d}"
        self._blobs[ref] = token
        self._meta[ref] = {
            "user_id": user_id,
            "owner_agent_id": self._owner(user_id),
            "provider": provider,
            "scope": scope,
        }
        self.store_calls.append({"user_id": user_id, "provider": provider, "scope": scope})
        return ref

    async def retrieve(self, secret_ref: str, requester_agent_id: str) -> bytes:
        self.retrieve_calls.append({"secret_ref": secret_ref, "requester": requester_agent_id})
        meta = self._meta.get(secret_ref)
        if meta is None or secret_ref not in self._blobs:
            raise KeyError(f"secret not found: {secret_ref}")
        if requester_agent_id != meta["owner_agent_id"]:
            raise PermissionError(
                f"credential isolation violation: owner={meta['owner_agent_id']} requester={requester_agent_id}"
            )
        return self._blobs[secret_ref]

    async def revoke(self, secret_ref: str) -> None:
        self.revoked.append(secret_ref)
        self._blobs.pop(secret_ref, None)
        self._meta.pop(secret_ref, None)

    def seed(self, user_id: str, bundle: bytes, ref: str = "secret_seed0001", scope: str = "test-scope") -> str:
        self._blobs[ref] = bundle
        self._meta[ref] = {
            "user_id": user_id,
            "owner_agent_id": self._owner(user_id),
            "provider": "google",
            "scope": scope,
        }
        return ref


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


class FakeHttpClient:
    """httpx.AsyncClient double — records calls, returns canned payloads."""

    def __init__(
        self,
        post_payload: dict | None = None,
        get_payload: dict | None = None,
        get_status: int = 200,
        post_status: int = 200,
    ) -> None:
        self.post_payload = post_payload or {}
        self.get_payload = get_payload if get_payload is not None else {"ok": True}
        self.get_status = get_status
        self.post_status = post_status
        self.posts: list[dict] = []
        self.gets: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, data=None, **kwargs):
        self.posts.append({"url": url, "data": dict(data or {}), "kwargs": kwargs})
        return FakeResponse(self.post_payload, self.post_status)

    async def get(self, url, headers=None, params=None, **kwargs):
        self.gets.append({"url": url, "headers": dict(headers or {}), "params": params, "kwargs": kwargs})
        return FakeResponse(self.get_payload, self.get_status)


def _factory_for(client: FakeHttpClient):
    return lambda **kwargs: client


def _ctx(user="employee:kim", agent="agent:assistant:kim", delegation_id="dlg_1", **extra):  # type: ignore[assignment]
    d: dict = {"user_id": user, "agent_id": agent, "tenant_id": "default"}
    if delegation_id:
        d["delegation_id"] = delegation_id
    d.update(extra)
    return d


@pytest.fixture(autouse=True)
def _nonprod_env(monkeypatch):
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT", "OAOS_MOCK_FALLBACK"):
        monkeypatch.delenv(k, raising=False)


def _live_bundle(access="ya29.live", refresh="1//rt", ttl=3600, scope="gmail-readonly"):
    return _encode_token_bundle(access, refresh, time.time() + ttl, scope)


def _expired_bundle(access="ya29.old", refresh="1//rt", scope="gmail-readonly"):
    return _encode_token_bundle(access, refresh, time.time() - 3600, scope)


# ---------------------------------------------------------------------------
# secret_ref resolution
# ---------------------------------------------------------------------------
class TestResolveSecretRef:
    def test_binding_map_by_delegation(self):
        c = GoogleConnector(credential_binding={"dlg_1": "secret_abc"})
        assert c.resolve_secret_ref(_ctx(delegation_id="dlg_1")) == "secret_abc"

    def test_no_binding_returns_none(self):
        c = GoogleConnector()
        assert c.resolve_secret_ref(_ctx(delegation_id="dlg_x")) is None

    def test_provider_dict(self):
        c = GoogleConnector(credential_provider={"dlg_9": "secret_p9"})
        assert c.resolve_secret_ref(_ctx(delegation_id="dlg_9")) == "secret_p9"

    async def test_async_provider_callable(self):
        async def prov(delegation_id, ctx):
            assert delegation_id == "dlg_a"
            return "secret_async1"

        c = GoogleConnector(credential_binding={}, credential_provider=prov)
        ref = await c._aresolve_secret_ref(_ctx(delegation_id="dlg_a"))
        assert ref == "secret_async1"

    def test_bind_unbind_helper(self):
        c = GoogleConnector()
        c.bind_credential("dlg_b", "secret_b")
        assert c.resolve_secret_ref(_ctx(delegation_id="dlg_b")) == "secret_b"
        c.unbind_credential("dlg_b")
        assert c.resolve_secret_ref(_ctx(delegation_id="dlg_b")) is None


# ---------------------------------------------------------------------------
# access token resolution + refresh
# ---------------------------------------------------------------------------
class TestResolveAccessToken:
    async def test_valid_bundle_no_http(self):
        vault = FakeVault()
        ref = vault.seed("employee:kim", _live_bundle())
        guard = FakeHttpClient()
        c = GoogleConnector(
            vault=vault, credential_binding={"dlg_1": ref},
            client_id="cid", client_secret="csec",
            http_client_factory=_factory_for(guard),
        )
        token, out_ref = await c.resolve_access_token(_ctx())
        assert token == "ya29.live"
        assert out_ref == ref
        assert guard.posts == []  # no refresh needed
        assert vault.store_calls == []

    async def test_legacy_bundle_no_expiry(self):
        vault = FakeVault()
        ref = vault.seed("employee:kim", b"legacy-access::legacy-refresh")
        c = GoogleConnector(vault=vault, credential_binding={"dlg_1": ref})
        token, _ = await c.resolve_access_token(_ctx())
        assert token == "legacy-access"

    async def test_expired_refreshes_and_persists_via_vault_only(self):
        vault = FakeVault()
        old_ref = vault.seed("employee:kim", _expired_bundle())
        http = FakeHttpClient(post_payload={"access_token": "ya29.new", "expires_in": 3600, "scope": "s"})
        c = GoogleConnector(
            vault=vault, credential_binding={"dlg_1": old_ref},
            client_id="cid", client_secret="csec",
            http_client_factory=_factory_for(http),
        )
        token, new_ref = await c.resolve_access_token(_ctx())
        assert token == "ya29.new"
        assert new_ref != old_ref
        # refreshed bundle persisted only via Vault.store, binding updated, old revoked
        assert len(vault.store_calls) == 1
        assert vault.store_calls[0]["user_id"] == "employee:kim"
        assert vault.store_calls[0]["provider"] == "google"
        assert c.resolve_secret_ref(_ctx()) == new_ref
        assert old_ref in vault.revoked
        # new bundle retrievable and live
        token2, _ = await c.resolve_access_token(_ctx())
        assert token2 == "ya29.new"
        # audit trail carries no secret values
        blob = repr(c.audit_events())
        assert "ya29.new" not in blob
        assert "1//rt" not in blob

    async def test_refresh_posts_token_endpoint_with_client_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "env-cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "env-csec")
        vault = FakeVault()
        old_ref = vault.seed("employee:kim", _expired_bundle())
        http = FakeHttpClient(post_payload={"access_token": "ya29.e", "expires_in": 100})
        c = GoogleConnector(vault=vault, credential_binding={"dlg_1": old_ref}, http_client_factory=_factory_for(http))
        await c.resolve_access_token(_ctx())
        assert len(http.posts) == 1
        post = http.posts[0]
        assert post["url"] == GOOGLE_TOKEN_URL
        assert post["data"]["grant_type"] == "refresh_token"
        assert post["data"]["client_id"] == "env-cid"
        assert post["data"]["refresh_token"] == "1//rt"

    async def test_refresh_without_client_config_fails_closed(self):
        vault = FakeVault()
        old_ref = vault.seed("employee:kim", _expired_bundle())
        http = FakeHttpClient(post_payload={"access_token": "ya29.x"})
        c = GoogleConnector(
            vault=vault, credential_binding={"dlg_1": old_ref},
            client_id="", client_secret="",
            http_client_factory=_factory_for(http),
        )
        with pytest.raises(RuntimeError, match="oauth client not configured"):
            await c.resolve_access_token(_ctx())

    async def test_expired_without_refresh_token_fails(self):
        vault = FakeVault()
        ref = vault.seed("employee:kim", _encode_token_bundle("ya29.old", None, time.time() - 10, "s"))
        c = GoogleConnector(vault=vault, credential_binding={"dlg_1": ref}, client_id="c", client_secret="s")
        with pytest.raises(RuntimeError, match="no refresh_token"):
            await c.resolve_access_token(_ctx())

    async def test_no_binding_raises_lookup(self):
        c = GoogleConnector(vault=FakeVault())
        with pytest.raises(LookupError, match="no credential binding"):
            await c.resolve_access_token(_ctx(delegation_id="dlg_missing"))

    async def test_no_vault_raises(self):
        c = GoogleConnector(credential_binding={"dlg_1": "secret_x"})
        with pytest.raises(RuntimeError, match="vault not configured"):
            await c.resolve_access_token(_ctx())

    async def test_owner_isolation_denied(self):
        vault = FakeVault()
        lee_ref = vault.seed("employee:lee", _live_bundle(access="ya29.lee"))
        http = FakeHttpClient(get_payload={"messages": []})
        c = GoogleConnector(
            vault=vault, credential_binding={"dlg_lee": lee_ref},
            client_id="c", client_secret="s",
            http_client_factory=_factory_for(http),
        )
        # kim's agent tries to use lee's bound secret_ref → Vault owner check DENY
        with pytest.raises(PermissionError, match="isolation violation"):
            await c.resolve_access_token(_ctx(user="employee:kim", agent="agent:assistant:kim", delegation_id="dlg_lee"))
        assert http.gets == []

    async def test_mocked_httpx_refresh_without_factory(self):
        """Refresh works through the real httpx import path when mocked."""
        vault = FakeVault()
        old_ref = vault.seed("employee:kim", _expired_bundle())
        c = GoogleConnector(vault=vault, credential_binding={"dlg_1": old_ref}, client_id="c", client_secret="s")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "ya29.mocked", "expires_in": 3600}
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("execution_gateway.connectors.google.httpx.AsyncClient", return_value=mock_client):
            token, _ = await c.resolve_access_token(_ctx())
        assert token == "ya29.mocked"


# ---------------------------------------------------------------------------
# read-only API path
# ---------------------------------------------------------------------------
class TestReadonlyApi:
    async def test_bearer_get_google_api(self):
        http = FakeHttpClient(get_payload={"messages": [{"id": "m1"}]})
        c = GoogleConnector(http_client_factory=_factory_for(http))
        out = await c.call_readonly_api("gmail_search", {"q": "hello"}, "ya29.live")
        assert out["via"] == "google_api"
        assert out["data"] == {"messages": [{"id": "m1"}]}
        assert len(http.gets) == 1
        call = http.gets[0]
        assert call["url"].startswith("https://gmail.googleapis.com/gmail/v1")
        assert call["headers"]["Authorization"] == "Bearer ya29.live"

    async def test_write_tool_refused(self):
        http = FakeHttpClient()
        c = GoogleConnector(http_client_factory=_factory_for(http))
        with pytest.raises(ValueError, match="not read-only"):
            await c.call_readonly_api("gmail_send", {"raw": "x"}, "ya29.live")
        assert http.gets == [] and http.posts == []

    def test_readonly_set_excludes_writes(self):
        for w in ("gmail_send", "calendar_create", "calendar_modify", "tasks_create", "tasks_modify"):
            assert w not in READONLY_TOOLS
        for r in ("gmail_search", "gmail_read", "calendar_list", "drive_search", "tasks_list"):
            assert r in READONLY_TOOLS


# ---------------------------------------------------------------------------
# call_via_gateway wiring: direct path vs skeleton vs fail-closed
# ---------------------------------------------------------------------------
class TestCallViaGateway:
    async def test_direct_path_for_readonly_with_credential(self):
        vault = FakeVault()
        ref = vault.seed("employee:kim", _live_bundle())
        http = FakeHttpClient(get_payload={"files": []})
        c = GoogleConnector(
            vault=vault, credential_binding={"dlg_1": ref},
            client_id="c", client_secret="s",
            http_client_factory=_factory_for(http),
        )
        out = await c.drive_search({"resource": "drive/user/kim/files"}, _ctx())
        assert out["via"] == "google_api"
        assert out["resource"] == "drive/user/kim/files"
        assert out["data"] == {"files": []}

    async def test_dev_skeleton_preserved_without_credential(self):
        c = GoogleConnector()
        out = await c.gmail_search({"q": "hi", "resource": "gmail/user/kim/messages"}, _ctx(delegation_id=None))
        assert out.get("via") == "fallback" or "request" in out or out.get("ok") is not None

    async def test_write_tool_never_direct(self):
        vault = FakeVault()
        ref = vault.seed("employee:kim", _live_bundle())
        http = FakeHttpClient(get_payload={"x": 1})
        c = GoogleConnector(
            vault=vault, credential_binding={"dlg_1": ref},
            client_id="c", client_secret="s",
            http_client_factory=_factory_for(http),
        )
        out = await c.gmail_send({"resource": "gmail/user/kim/messages/1", "raw": "hi"}, _ctx())
        assert out.get("via") != "google_api"
        assert http.gets == []

    async def test_401_triggers_single_refresh_retry(self):
        vault = FakeVault()
        ref = vault.seed("employee:kim", _live_bundle(access="ya29.stale", refresh="1//rt"))

        gets = {"n": 0}

        class FlakyClient(FakeHttpClient):
            async def get(self, url, headers=None, params=None, **kwargs):
                self.gets.append({"url": url, "headers": dict(headers or {}), "params": params})
                gets["n"] += 1
                if gets["n"] == 1:
                    return FakeResponse({"error": "unauthorized"}, 401)
                return FakeResponse({"messages": ["m1"]}, 200)

        http = FlakyClient(post_payload={"access_token": "ya29.fresh", "expires_in": 3600})
        c = GoogleConnector(
            vault=vault, credential_binding={"dlg_1": ref},
            client_id="c", client_secret="s",
            http_client_factory=_factory_for(http),
        )
        out = await c.gmail_search({"resource": "gmail/user/kim/messages"}, _ctx())
        assert out["via"] == "google_api"
        assert out["data"] == {"messages": ["m1"]}
        assert len(http.posts) == 1  # exactly one refresh retry

    async def test_production_fail_closed_no_binding(self, monkeypatch):
        monkeypatch.setenv("OAOS_ENV", "production")
        c = GoogleConnector(vault=FakeVault(), client_id="c", client_secret="s")
        with pytest.raises(PermissionError, match="no credential"):
            await c.gmail_search({"resource": "gmail/user/kim/messages"}, _ctx(delegation_id="dlg_missing"))

    async def test_production_fail_closed_no_vault(self, monkeypatch):
        monkeypatch.setenv("OAOS_ENV", "production")
        c = GoogleConnector()  # nothing injected
        with pytest.raises(PermissionError, match="no credential"):
            await c.gmail_search({"resource": "gmail/user/kim/messages"}, _ctx(delegation_id="dlg_1"))

    async def test_owner_deny_propagates_fail_closed(self, monkeypatch):
        monkeypatch.setenv("OAOS_ENV", "production")
        vault = FakeVault()
        lee_ref = vault.seed("employee:lee", _live_bundle(access="ya29.lee"))
        c = GoogleConnector(vault=vault, credential_binding={"dlg_lee": lee_ref}, client_id="c", client_secret="s")
        with pytest.raises(PermissionError):
            await c.gmail_search(
                {"resource": "gmail/user/kim/messages"},
                _ctx(user="employee:kim", agent="agent:assistant:kim", delegation_id="dlg_lee"),
            )


# ---------------------------------------------------------------------------
# delegation validation gate
# ---------------------------------------------------------------------------
class TestValidateDelegation:
    def test_dev_permissive_without_binding(self):
        c = GoogleConnector()
        ok, _ = c.validate_delegation(_ctx(delegation_id="dlg_1"), "gmail/user/kim/messages/1")
        assert ok is True

    def test_ok_with_binding(self):
        c = GoogleConnector(credential_binding={"dlg_1": "secret_x"})
        ok, _ = c.validate_delegation(_ctx(delegation_id="dlg_1"), "gmail/user/kim/messages/1")
        assert ok is True

    def test_production_deny_without_binding(self, monkeypatch):
        monkeypatch.setenv("OAOS_ENV", "production")
        c = GoogleConnector()
        ok, reason = c.validate_delegation(_ctx(delegation_id="dlg_1"), "gmail/user/kim/messages/1")
        assert ok is False
        assert "fail-closed" in reason
