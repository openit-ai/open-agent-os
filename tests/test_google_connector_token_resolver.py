"""GoogleConnector token resolver + live read-only path (Section 9-10).

Fake-vault + fake-httpx coverage for the connector-side integration:
- owner-scoped secret_ref binding lookup via Vault.retrieve (access::refresh bundles)
- real httpx read-only calls (calendar_list, gmail_search)
- 401/expiry refresh via oauth2.googleapis.com/token + Vault re-store + binding update
- production fail-closed on missing vault/binding/token (no mock/planned success)
- owner mismatch DENY; nonprod skeleton preserved; writes never take the direct path
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in [
    ROOT / "execution-gateway",
    ROOT / "security/credential-vault",
    ROOT / "security/delegation",
    ROOT / "packages/agent-runtime",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from execution_gateway.connectors.google import GoogleConnector  # type: ignore


def _ctx(user="employee:kim", agent="agent:assistant:kim", delegation_id="dlg_1"):
    d = {"user_id": user, "agent_id": agent, "tenant_id": "default"}
    if delegation_id is not None:
        d["delegation_id"] = delegation_id
    return d


def _nonprod(monkeypatch):
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OAOS_ENV", "development")


def _prod(monkeypatch):
    for k in ("ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OAOS_ENV", "production")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeVault:
    """Duck-typed CredentialVault: owner-isolated, encrypted-bundle semantics."""

    def __init__(self):
        self._store: dict[str, dict] = {}
        self._counter = 0
        self.revoked: list[str] = []
        self.store_calls: list[dict] = []

    @staticmethod
    def _owner_agent(user_id: str) -> str:
        suffix = user_id.split(":", 1)[-1] if ":" in user_id else user_id
        return f"agent:assistant:{suffix}"

    async def store(self, user_id: str, provider: str, scope: str, token: bytes) -> str:
        self._counter += 1
        ref = f"fake-secret-{self._counter}"
        self._store[ref] = {"user_id": user_id, "provider": provider, "scope": scope, "token": token}
        self.store_calls.append({"ref": ref, "user_id": user_id, "provider": provider, "scope": scope})
        return ref

    async def retrieve(self, secret_ref: str, requester_agent_id: str) -> bytes:
        meta = self._store.get(secret_ref)
        if meta is None:
            raise KeyError(f"unknown secret_ref: {secret_ref}")
        if requester_agent_id != self._owner_agent(meta["user_id"]):
            raise PermissionError(f"isolation violation: {requester_agent_id} cannot access {meta['user_id']} credential")
        return meta["token"]

    async def revoke(self, secret_ref: str) -> None:
        self.revoked.append(secret_ref)
        self._store.pop(secret_ref, None)


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def raise_for_status(self):
        if 400 <= self.status_code < 600:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        if self.status_code == 401:
            raise ValueError("no json on 401")
        return self._payload


class FakeGoogleHttp:
    """httpx.AsyncClient stand-in routing by URL. Never touches the network."""

    TOKEN_URL = "https://oauth2.googleapis.com/token"

    def __init__(self, get_handler=None, post_handler=None):
        self.get_calls: list[dict] = []
        self.post_calls: list[dict] = []
        self._get_handler = get_handler or (lambda url, headers, params: FakeResp(200, {}))
        self._post_handler = post_handler or (lambda url, data: FakeResp(200, {}))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers=None, params=None):
        self.get_calls.append({"url": url, "headers": dict(headers or {}), "params": params})
        return self._get_handler(url, headers or {}, params)

    async def post(self, url, data=None):
        self.post_calls.append({"url": url, "data": dict(data or {})})
        return self._post_handler(url, data or {})


def _connector(vault, http, **kw):
    kw.setdefault("client_id", "cid")
    kw.setdefault("client_secret", "csecret")
    return GoogleConnector(vault=vault, http_client_factory=lambda timeout=15: http, **kw)


# ---------------------------------------------------------------------------
# Live read-only calls
# ---------------------------------------------------------------------------
class TestLiveReadOnly:
    @pytest.mark.asyncio
    async def test_calendar_list_live_via_vault(self, monkeypatch):
        _nonprod(monkeypatch)
        vault = FakeVault()
        bundle = json.dumps({"access_token": "live-at", "refresh_token": "rt", "expires_at": time.time() + 3600, "scope": "s"}).encode()
        ref = await vault.store("employee:kim", "google", "s", bundle)
        seen: dict = {}

        def get_handler(url, headers, params):
            seen.update(headers)
            assert url == "https://www.googleapis.com/calendar/v3/users/me/calendarList"
            return FakeResp(200, {"items": [{"id": "cal-1"}]})

        http = FakeGoogleHttp(get_handler=get_handler)
        gc = _connector(vault, http)
        gc.bind_credential("dlg_1", ref)
        result = await gc.calendar_list({"resource": "calendar/user/kim/events"}, _ctx())
        assert result["via"] == "google_api"
        assert result["data"] == {"items": [{"id": "cal-1"}]}
        assert result["resource"] == "calendar/user/kim/events"
        assert seen.get("Authorization") == "Bearer live-at"
        # secrets never hit the audit trail
        assert "live-at" not in json.dumps(gc.audit_events())

    @pytest.mark.asyncio
    async def test_gmail_search_live_legacy_bundle(self, monkeypatch):
        _nonprod(monkeypatch)
        vault = FakeVault()
        ref = await vault.store("employee:kim", "google", "s", b"tok-abc::ref-xyz")
        seen: dict = {}

        def get_handler(url, headers, params):
            seen.update(headers)
            assert url == "https://gmail.googleapis.com/gmail/v1/users/me/messages"
            assert (params or {}).get("q") == "hello"
            return FakeResp(200, {"messages": [{"id": "m1"}]})

        http = FakeGoogleHttp(get_handler=get_handler)
        gc = _connector(vault, http)
        gc.bind_credential("dlg_1", ref)
        result = await gc.gmail_search({"q": "hello", "resource": "gmail/user/kim/messages"}, _ctx())
        assert result["via"] == "google_api"
        assert result["data"] == {"messages": [{"id": "m1"}]}
        assert seen.get("Authorization") == "Bearer tok-abc"


# ---------------------------------------------------------------------------
# Refresh paths
# ---------------------------------------------------------------------------
class TestRefresh:
    @pytest.mark.asyncio
    async def test_expired_bundle_refreshes_via_env_and_restores(self, monkeypatch):
        _nonprod(monkeypatch)
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "env-cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "env-csec")
        vault = FakeVault()
        expired = json.dumps({"access_token": "old-at", "refresh_token": "old-rt", "expires_at": time.time() - 10, "scope": "s"}).encode()
        old_ref = await vault.store("employee:kim", "google", "s", expired)
        provider = {"dlg_1": old_ref}
        tokens: dict = {}

        def post_handler(url, data):
            assert url == "https://oauth2.googleapis.com/token"
            assert data["client_id"] == "env-cid" and data["client_secret"] == "env-csec"
            assert data["refresh_token"] == "old-rt" and data["grant_type"] == "refresh_token"
            return FakeResp(200, {"access_token": "new-at", "refresh_token": "new-rt", "expires_in": 3600})

        def get_handler(url, headers, params):
            tokens.update(headers)
            assert headers.get("Authorization") == "Bearer new-at"
            return FakeResp(200, {"items": []})

        http = FakeGoogleHttp(get_handler=get_handler, post_handler=post_handler)
        gc = GoogleConnector(vault=vault, credential_provider=provider, http_client_factory=lambda timeout=15: http)
        result = await gc.calendar_list({"resource": "calendar/user/kim/events"}, _ctx())
        assert result["via"] == "google_api"
        # re-stored via vault.store, binding updated everywhere, old revoked best-effort
        assert len(vault.store_calls) == 2
        assert vault.store_calls[1]["user_id"] == "employee:kim"
        new_ref = provider["dlg_1"]
        assert new_ref != old_ref
        assert gc.resolve_secret_ref(_ctx()) == new_ref
        assert old_ref in vault.revoked
        raw = await vault.retrieve(new_ref, "agent:assistant:kim")
        assert json.loads(raw.decode())["access_token"] == "new-at"

    @pytest.mark.asyncio
    async def test_401_triggers_single_refresh_retry(self, monkeypatch):
        _nonprod(monkeypatch)
        vault = FakeVault()
        bundle = json.dumps({"access_token": "stale-at", "refresh_token": "rt-1", "expires_at": time.time() + 3600, "scope": "s"}).encode()
        old_ref = await vault.store("employee:kim", "google", "s", bundle)

        def get_handler(url, headers, params):
            if headers.get("Authorization") == "Bearer stale-at":
                return FakeResp(401)
            assert headers.get("Authorization") == "Bearer fresh-at"
            return FakeResp(200, {"messages": [{"id": "m9"}]})

        def post_handler(url, data):
            assert data["refresh_token"] == "rt-1"
            return FakeResp(200, {"access_token": "fresh-at", "expires_in": 3600, "scope": "s"})

        http = FakeGoogleHttp(get_handler=get_handler, post_handler=post_handler)
        gc = _connector(vault, http)
        gc.bind_credential("dlg_1", old_ref)
        result = await gc.gmail_search({"q": "x", "resource": "gmail/user/kim/messages"}, _ctx())
        assert result["via"] == "google_api"
        assert result["data"] == {"messages": [{"id": "m9"}]}
        assert len(http.post_calls) == 1  # exactly one refresh attempt
        assert gc.resolve_secret_ref(_ctx()) != old_ref


# ---------------------------------------------------------------------------
# Fail-closed vs skeleton
# ---------------------------------------------------------------------------
class TestFailClosed:
    @pytest.mark.asyncio
    async def test_missing_binding_production_fail_closed(self, monkeypatch):
        _prod(monkeypatch)
        vault = FakeVault()
        http = FakeGoogleHttp()
        gc = _connector(vault, http)
        with pytest.raises(PermissionError, match="no credential"):
            await gc.calendar_list({"resource": "calendar/user/kim/events"}, _ctx(delegation_id="dlg_missing"))
        # no mock/planned success, no Google API attempted
        assert http.get_calls == [] and http.post_calls == []

    @pytest.mark.asyncio
    async def test_missing_vault_production_fail_closed(self, monkeypatch):
        _prod(monkeypatch)
        gc = GoogleConnector()
        with pytest.raises(PermissionError, match="no credential"):
            await gc.gmail_search({"q": "x", "resource": "gmail/user/kim/messages"}, _ctx(delegation_id="dlg_1"))

    @pytest.mark.asyncio
    async def test_owner_mismatch_denied(self, monkeypatch):
        _nonprod(monkeypatch)
        vault = FakeVault()
        ref = await vault.store("employee:kim", "google", "s", b"tok::ref")
        http = FakeGoogleHttp()
        gc = _connector(vault, http)
        gc.bind_credential("dlg_1", ref)
        with pytest.raises(PermissionError, match="owner mismatch"):
            await gc.gmail_search({"resource": "gmail/user/lee/messages"}, _ctx())
        assert http.get_calls == [] and http.post_calls == []

    @pytest.mark.asyncio
    async def test_nonprod_skeleton_preserved_without_vault(self, monkeypatch):
        _nonprod(monkeypatch)
        gc = GoogleConnector()
        result = await gc.gmail_search({"q": "hello", "resource": "gmail/user/kim/messages"}, _ctx(delegation_id=None))
        assert "tool" in result or "ok" in result or "request" in result

    @pytest.mark.asyncio
    async def test_write_never_takes_direct_path(self, monkeypatch):
        _nonprod(monkeypatch)
        vault = FakeVault()
        ref = await vault.store("employee:kim", "google", "s", b"tok::ref")
        http = FakeGoogleHttp()
        gc = _connector(vault, http)
        gc.bind_credential("dlg_1", ref)
        result = await gc.gmail_send({"resource": "gmail/user/kim/messages/1", "raw": "hi"}, _ctx())
        assert result.get("via") != "google_api"
        assert http.get_calls == [] and http.post_calls == []

    def test_never_reads_hermes_token_files(self):
        src = Path(__file__).resolve().parents[1] / "execution-gateway/execution_gateway/connectors/google.py"
        text = src.read_text()
        assert "/home/hermes" not in text
        assert "token.json" not in text
        assert "credentials.json" not in text
