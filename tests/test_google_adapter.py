"""Google adapter production tests — OAuth scopes, owner isolation, revoke, rate limit, refresh, audit.
Covers §§9-10 personal delegation, §10.2 owner isolation, §16H rate limit, Execution Gateway wrappers.
"""
import sys
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
for p in [
    ROOT / "adapters",
    ROOT / "execution-gateway",
    ROOT / "security/credential-vault",
    ROOT / "security/delegation",
    ROOT / "packages/delegation-model",
    ROOT / "packages/common-types",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
if str(ROOT / "security") not in sys.path:
    sys.path.insert(0, str(ROOT / "security"))

from google.adapter import GoogleAdapter, GOOGLE_SCOPES, GOOGLE_AUTH_URL  # type: ignore
from vault.vault import EncryptedPostgresVault  # type: ignore
from delegation_service.service import DelegationService  # type: ignore
from execution_gateway.connectors.google import GoogleConnector  # type: ignore


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _ctx(user="employee:kim", agent="agent:assistant:kim", delegation_id=None, tenant="default", scope=""):
    d: dict = {"user_id": user, "agent_id": agent, "tenant_id": tenant}
    if delegation_id:
        d["delegation_id"] = delegation_id
    if scope:
        d["scope"] = scope
        d["granted_scope"] = scope
    return d


@pytest.fixture
def vault():
    return EncryptedPostgresVault(encryption_key=b"test-key-32-bytes-for-google-adapter")


@pytest.fixture
def delegation_service():
    return DelegationService()


@pytest.fixture
def adapter(vault, delegation_service):
    return GoogleAdapter(
        client_id="cid", client_secret="csecret", redirect_uri="http://localhost:8080/callback",
        vault=vault, delegation_service=delegation_service,
    )


@pytest.fixture
def connector():
    return GoogleConnector(rate_limit_per_sec=10, burst=20)


# ---------------------------------------------------------------------------
# OAuth scopes
# ---------------------------------------------------------------------------
class TestOAuthScopes:
    def test_authorize_url_default_scopes_least_privilege(self, adapter):
        url, state = adapter.authorize_url("dlg_1", "employee:kim")
        assert GOOGLE_AUTH_URL in url
        # default should be readonly only, no write scopes
        assert "gmail.readonly" in url
        assert "calendar.readonly" in url
        assert "drive.readonly" in url
        # should NOT contain gmail.send by default (least privilege)
        assert "gmail.send" not in url
        assert state in adapter._states

    def test_authorize_url_explicit_scopes(self, adapter):
        scopes = [GOOGLE_SCOPES["gmail_send"], GOOGLE_SCOPES["calendar_create"]]
        url, state = adapter.authorize_url("dlg_2", "employee:kim", scopes=scopes)
        assert "gmail.send" in url
        # calendar full scope
        assert "calendar" in url

    def test_authorize_url_invalid_scope_rejected(self, adapter):
        with pytest.raises(ValueError, match="invalid scope"):
            adapter.authorize_url("dlg_x", "employee:kim", scopes=["not-a-scope"])

    def test_required_scope_mapping(self, adapter):
        assert adapter.required_scope("gmail_search") == GOOGLE_SCOPES["gmail_search"]
        assert adapter.required_scope("calendar_create") == GOOGLE_SCOPES["calendar_create"]
        assert adapter.required_scope("drive_search") == GOOGLE_SCOPES["drive_search"]
        assert adapter.required_scope("tasks_list") == GOOGLE_SCOPES["tasks_list"]
        assert adapter.required_scope("unknown_tool") is None

    def test_validate_scope_ok(self, adapter):
        ok, _ = adapter.validate_scope("gmail_search", GOOGLE_SCOPES["gmail_search"])
        assert ok is True
        # multiple scopes string
        scope_str = f"{GOOGLE_SCOPES['gmail_search']} {GOOGLE_SCOPES['calendar_read']}"
        ok2, _ = adapter.validate_scope("gmail_search", scope_str)
        assert ok2 is True

    def test_validate_scope_mismatch(self, adapter):
        ok, reason = adapter.validate_scope("gmail_send", GOOGLE_SCOPES["gmail_search"])
        assert ok is False
        assert "mismatch" in reason.lower()
        ok2, _ = adapter.validate_scope("gmail_send", "")
        assert ok2 is False

    def test_is_valid_scope_string(self, adapter):
        assert adapter.is_valid_scope_string(GOOGLE_SCOPES["gmail_search"]) is True
        assert adapter.is_valid_scope_string(f"{GOOGLE_SCOPES['gmail_search']} openid") is True
        assert adapter.is_valid_scope_string("") is False

    def test_describe_tools_has_all(self, adapter):
        tools = adapter.describe_tools()
        names = {t["name"] for t in tools}
        assert names == set(GOOGLE_SCOPES.keys())
        for t in tools:
            assert "scope" in t and "domain" in t

    @pytest.mark.asyncio
    async def test_exchange_code_stores_vault_and_binding(self, adapter):
        # create delegation first
        d = adapter.delegation_service.grant("employee:kim", "agent:assistant:kim", "google", GOOGLE_SCOPES["gmail_read"])
        url, state = adapter.authorize_url(d.id, "employee:kim", scopes=[GOOGLE_SCOPES["gmail_read"]])
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "ya29.accesstoken123",
            "refresh_token": "1//refresh123",
            "scope": GOOGLE_SCOPES["gmail_read"],
            "expires_in": 3600,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("google.adapter.httpx.AsyncClient", return_value=mock_client):
            result = await adapter.exchange_code("authcode123", state)
        assert result["delegation_id"] == d.id
        assert result["secret_ref"] is not None
        assert result["has_refresh_token"] is True
        assert d.id in adapter._binding
        # vault should hold encrypted token
        secret_ref = result["secret_ref"]
        token = await adapter.get_access_token(secret_ref, "agent:assistant:kim")
        assert token == "ya29.accesstoken123"
        # binding exists in delegation_service
        # is_active should be true
        assert adapter.delegation_service.is_active(d.id) is True

    @pytest.mark.asyncio
    async def test_exchange_code_invalid_state(self, adapter):
        with pytest.raises(ValueError, match="invalid or expired state"):
            await adapter.exchange_code("code", "badstate123")

    def test_tool_domain_and_action(self, adapter):
        assert adapter.tool_domain("gmail_send") == "gmail"
        assert adapter.tool_domain("drive_read") == "drive"
        assert adapter.tool_action("gmail_send") == "SEND"
        assert adapter.tool_action("calendar_create") == "CREATE"


# ---------------------------------------------------------------------------
# Owner isolation — kim cannot use lee token DENY (§10.2)
# ---------------------------------------------------------------------------
class TestOwnerIsolation:
    def test_check_owner_kim_ok(self, adapter):
        res = adapter.check_owner(_ctx("employee:kim", "agent:assistant:kim"), "gmail/user/kim/messages/123")
        assert res.allowed is True

    def test_check_owner_kim_cannot_access_lee_resource_DENY(self, adapter):
        res = adapter.check_owner(_ctx("employee:kim", "agent:assistant:kim"), "gmail/user/lee/messages/123")
        assert res.allowed is False
        assert "owner mismatch" in res.reason

    def test_check_owner_calendar_isolation(self, adapter):
        res = adapter.check_owner(_ctx("employee:lee", "agent:assistant:lee"), "calendar/user/kim/events/1")
        assert res.allowed is False

    def test_check_owner_agent_mismatch_DENY(self, adapter):
        # agent:assistant:lee trying to access employee:kim resource via kim's user but lee's agent
        res = adapter.check_owner(_ctx("employee:kim", "agent:assistant:lee"), "gmail/user/kim/messages/1")
        assert res.allowed is False
        assert "agent mismatch" in res.reason

    def test_check_owner_drive_ok(self, adapter):
        res = adapter.check_owner(_ctx("employee:kim", "agent:assistant:kim"), "drive/user/kim/files/abc")
        assert res.allowed is True

    def test_check_owner_missing_user_DENY(self, adapter):
        res = adapter.check_owner({}, "gmail/user/kim/messages/1")
        assert res.allowed is False

    @pytest.mark.asyncio
    async def test_vault_isolation_kim_cannot_retrieve_lee_token_DENY(self, vault):
        # store as lee
        ref = await vault.store("employee:lee", "google", GOOGLE_SCOPES["gmail_read"], b"lee-token::refresh-lee")
        # kim's agent cannot retrieve
        with pytest.raises(PermissionError, match="isolation violation"):
            await vault.retrieve(ref, "agent:assistant:kim")
        # lee's agent can
        data = await vault.retrieve(ref, "agent:assistant:lee")
        assert b"lee-token" in data

    @pytest.mark.asyncio
    async def test_call_tool_owner_mismatch_raises(self, adapter):
        d = adapter.delegation_service.grant("employee:kim", "agent:assistant:kim", "google", GOOGLE_SCOPES["gmail_read"])
        # try to call tool for lee's resource as kim → DENY
        with pytest.raises(PermissionError, match="owner mismatch"):
            await adapter.call_tool("gmail_read", {"resource": "gmail/user/lee/messages/1"}, _ctx("employee:kim", "agent:assistant:kim", d.id))

    @pytest.mark.asyncio
    async def test_vault_token_owner_isolation_via_adapter(self, adapter):
        d = adapter.delegation_service.grant("employee:lee", "agent:assistant:lee", "google", GOOGLE_SCOPES["gmail_read"])
        url, state = adapter.authorize_url(d.id, "employee:lee", scopes=[GOOGLE_SCOPES["gmail_read"]])
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "lee-access", "refresh_token": "lee-refresh", "scope": GOOGLE_SCOPES["gmail_read"], "expires_in": 3600}
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("google.adapter.httpx.AsyncClient", return_value=mock_client):
            result = await adapter.exchange_code("code", state)
        secret_ref = result["secret_ref"]
        # kim cannot get lee's token via adapter
        with pytest.raises(PermissionError):
            await adapter.get_access_token(secret_ref, "agent:assistant:kim")
        # lee can
        tok = await adapter.get_access_token(secret_ref, "agent:assistant:lee")
        assert tok == "lee-access"

    def test_connector_owner_check(self, connector):
        res = connector.check_owner(_ctx("employee:kim", "agent:assistant:kim"), "gmail/user/kim/messages/1")
        assert res.allowed is True
        res2 = connector.check_owner(_ctx("employee:kim", "agent:assistant:kim"), "gmail/user/lee/messages/1")
        assert res2.allowed is False
        assert "owner mismatch" in res2.reason


# ---------------------------------------------------------------------------
# Revoke — immediate revoke → invalidate capabilities
# ---------------------------------------------------------------------------
class TestRevoke:
    @pytest.mark.asyncio
    async def test_revoke_delegation_immediate_invalidation(self, adapter):
        d = adapter.delegation_service.grant("employee:kim", "agent:assistant:kim", "google", GOOGLE_SCOPES["gmail_read"])
        # bind fake secret
        adapter._binding[d.id] = "secret_abc123"
        adapter._revoked_delegations.clear()
        result = await adapter.revoke_delegation(d.id, token="ya29.token123")
        assert result["delegation_id"] == d.id
        assert adapter.is_revoked(delegation_id=d.id) is True
        assert d.id in adapter._revoked_delegations
        # subsequent call_tool should DENY immediately
        with pytest.raises(PermissionError, match="delegation revoked"):
            await adapter.call_tool("gmail_read", {"resource": "gmail/user/kim/messages/1"}, _ctx("employee:kim", "agent:assistant:kim", d.id))

    @pytest.mark.asyncio
    async def test_revoke_cascade_vault_and_delegation(self, adapter, vault):
        d = adapter.delegation_service.grant("employee:kim", "agent:assistant:kim", "google", GOOGLE_SCOPES["gmail_read"])
        ref = await vault.store("employee:kim", "google", GOOGLE_SCOPES["gmail_read"], b"access::refresh")
        adapter.vault = vault
        adapter._binding[d.id] = ref
        # also create binding in delegation_service
        b = adapter.delegation_service.bind_credential(d.id, "google", ref, GOOGLE_SCOPES["gmail_read"])
        assert adapter.delegation_service.is_binding_active(b.id) is True
        await adapter.revoke_delegation(d.id, token="tok123")
        # delegation revoked
        assert adapter.delegation_service.is_active(d.id) is False
        # binding cascade revoked
        assert adapter.delegation_service.is_binding_active(b.id) is False
        # vault revoked
        with pytest.raises(KeyError):
            await vault.retrieve(ref, "agent:assistant:kim")

    @pytest.mark.asyncio
    async def test_get_access_token_after_revoke_DENY(self, adapter, vault):
        d = adapter.delegation_service.grant("employee:kim", "agent:assistant:kim", "google", GOOGLE_SCOPES["gmail_read"])
        ref = await vault.store("employee:kim", "google", GOOGLE_SCOPES["gmail_read"], b"tok::ref")
        adapter.vault = vault
        adapter._binding[d.id] = ref
        await adapter.revoke_delegation(d.id)
        with pytest.raises(PermissionError, match="revoked"):
            await adapter.get_access_token(ref, "agent:assistant:kim")

    @pytest.mark.asyncio
    async def test_revoke_token_adds_to_revoked_set(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("google.adapter.httpx.AsyncClient", return_value=mock_client):
            ok = await adapter.revoke("ya29.testtoken")
        assert ok is True
        assert "ya29.testtoken" in adapter._revoked_tokens

    @pytest.mark.asyncio
    async def test_refresh_after_revoke_DENY(self, adapter, vault):
        d = adapter.delegation_service.grant("employee:kim", "agent:assistant:kim", "google", GOOGLE_SCOPES["gmail_read"])
        ref = await vault.store("employee:kim", "google", GOOGLE_SCOPES["gmail_read"], b"access::refresh123")
        adapter.vault = vault
        adapter._binding[d.id] = ref
        await adapter.revoke_delegation(d.id)
        with pytest.raises(PermissionError, match="delegation revoked"):
            await adapter.refresh_for_delegation(d.id, "agent:assistant:kim")


# ---------------------------------------------------------------------------
# Rate limit (§16H)
# ---------------------------------------------------------------------------
class TestRateLimit:
    def test_rate_limit_allows_burst(self, adapter):
        ctx = _ctx("employee:kim", "agent:assistant:kim")
        # burst 20 should allow 20
        for i in range(20):
            allowed, _ = adapter.check_rate_limit(ctx, "gmail_search")
            assert allowed is True, f"failed at {i}"

    def test_rate_limit_blocks_excess(self, adapter):
        ctx = _ctx("employee:kim", "agent:assistant:kim")
        # exhaust burst
        for _ in range(20):
            adapter.check_rate_limit(ctx, "drive_search")
        allowed, retry = adapter.check_rate_limit(ctx, "drive_search")
        assert allowed is False
        assert retry >= 0

    @pytest.mark.asyncio
    async def test_call_tool_rate_limited(self, adapter):
        ctx = _ctx("employee:kim", "agent:assistant:kim")
        # artificially exhaust rate limiter for this tool
        for _ in range(25):
            adapter.check_rate_limit(ctx, "gmail_send")
        with pytest.raises(RuntimeError, match="rate limited"):
            await adapter.call_tool("gmail_send", {"resource": "gmail/user/kim/messages/1", "raw": "test"}, ctx, access_token="tok")

    def test_connector_rate_limit(self, connector):
        ctx = _ctx("employee:kim", "agent:assistant:kim")
        for _ in range(20):
            ok, _ = connector.check_rate_limit(ctx, "gmail_search")
            assert ok is True
        ok, _ = connector.check_rate_limit(ctx, "gmail_search")
        assert ok is False

    def test_rate_limit_per_user_isolated(self, adapter):
        ctx_kim = _ctx("employee:kim", "agent:assistant:kim")
        ctx_lee = _ctx("employee:lee", "agent:assistant:lee")
        for _ in range(20):
            adapter.check_rate_limit(ctx_kim, "gmail_search")
        # kim blocked
        allowed_kim, _ = adapter.check_rate_limit(ctx_kim, "gmail_search")
        assert allowed_kim is False
        # lee still allowed
        allowed_lee, _ = adapter.check_rate_limit(ctx_lee, "gmail_search")
        assert allowed_lee is True


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------
class TestTokenRefresh:
    @pytest.mark.asyncio
    async def test_refresh_exchanges_token(self, adapter):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "new_access", "expires_in": 3600, "scope": GOOGLE_SCOPES["gmail_read"]}
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("google.adapter.httpx.AsyncClient", return_value=mock_client):
            ts = await adapter.refresh("old_refresh_token")
        assert ts.access_token == "new_access"
        assert ts.refresh_token == "old_refresh_token"  # preserved

    @pytest.mark.asyncio
    async def test_refresh_for_delegation(self, adapter, vault):
        d = adapter.delegation_service.grant("employee:kim", "agent:assistant:kim", "google", GOOGLE_SCOPES["gmail_read"])
        ref = await vault.store("employee:kim", "google", GOOGLE_SCOPES["gmail_read"], b"old_access::my_refresh")
        adapter.vault = vault
        adapter._binding[d.id] = ref
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "refreshed_access", "expires_in": 3600, "scope": GOOGLE_SCOPES["gmail_read"]}
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("google.adapter.httpx.AsyncClient", return_value=mock_client):
            ts = await adapter.refresh_for_delegation(d.id, "agent:assistant:kim")
        assert ts.access_token == "refreshed_access"
        # new binding should exist
        new_ref = adapter._binding[d.id]
        assert new_ref != ref
        tok = await adapter.get_access_token(new_ref, "agent:assistant:kim")
        assert tok == "refreshed_access"

    @pytest.mark.asyncio
    async def test_refresh_for_delegation_no_refresh_token_error(self, adapter, vault):
        d = adapter.delegation_service.grant("employee:kim", "agent:assistant:kim", "google", GOOGLE_SCOPES["gmail_read"])
        ref = await vault.store("employee:kim", "google", GOOGLE_SCOPES["gmail_read"], b"only_access::")
        adapter.vault = vault
        adapter._binding[d.id] = ref
        with pytest.raises(ValueError, match="no refresh_token"):
            await adapter.refresh_for_delegation(d.id, "agent:assistant:kim")


# ---------------------------------------------------------------------------
# Connector wrappers — scope validation, rate limit, audit, gateway path
# ---------------------------------------------------------------------------
class TestConnectorWrappers:
    @pytest.mark.asyncio
    async def test_connector_gmail_search_via_gateway(self, connector):
        ctx = _ctx("employee:kim", "agent:assistant:kim")
        result = await connector.gmail_search({"q": "hello", "resource": "gmail/user/kim/messages"}, ctx)
        # should return either gateway result or fallback planned
        assert "tool" in result or "ok" in result or "request" in result

    @pytest.mark.asyncio
    async def test_connector_scope_validation_DENY(self, connector):
        ctx = _ctx("employee:kim", "agent:assistant:kim", scope=GOOGLE_SCOPES["gmail_read"])
        # gmail_send requires gmail.send scope, but granted is gmail.readonly → DENY
        with pytest.raises(PermissionError, match="scope mismatch"):
            await connector.gmail_send({"resource": "gmail/user/kim/messages/1", "raw": "hi"}, ctx)

    @pytest.mark.asyncio
    async def test_connector_owner_DENY(self, connector):
        ctx = _ctx("employee:kim", "agent:assistant:kim")
        with pytest.raises(PermissionError, match="owner mismatch"):
            await connector.gmail_search({"resource": "gmail/user/lee/messages"}, ctx)

    @pytest.mark.asyncio
    async def test_connector_rate_limited(self, connector):
        ctx = _ctx("employee:kim", "agent:assistant:kim")
        for _ in range(25):
            connector.check_rate_limit(ctx, "drive_search")
        with pytest.raises(RuntimeError, match="rate limited"):
            await connector.drive_search({"resource": "drive/user/kim/files"}, ctx)

    @pytest.mark.asyncio
    async def test_adapter_wrappers_exist(self, adapter):
        for name in ["gmail_search", "gmail_read", "gmail_send", "calendar_list", "drive_search", "tasks_list"]:
            assert hasattr(adapter, name)
            assert callable(getattr(adapter, name))

    @pytest.mark.asyncio
    async def test_adapter_call_tool_audit_logged(self, adapter):
        ctx = _ctx("employee:kim", "agent:assistant:kim")
        # call without token returns planned but audits
        result = await adapter.call_tool("gmail_search", {"q": "test"}, ctx, access_token="tok123")
        assert result["tool"] == "gmail_search"
        events = adapter.audit_events()
        # should have at least attempt + success
        types = [e["event_type"] for e in events]
        assert "TOOL_CALL_ATTEMPT" in types

    @pytest.mark.asyncio
    async def test_connector_audit_logged(self, connector):
        ctx = _ctx("employee:kim", "agent:assistant:kim")
        await connector.calendar_list({"resource": "calendar/user/kim/events"}, ctx)
        events = connector.audit_events()
        assert len(events) > 0
        assert any("TOOL_CALL_ATTEMPT" in e["event_type"] for e in events)

    def test_connector_list_tools(self, connector):
        tools = connector.list_tools()
        assert "gmail_search" in tools
        assert "calendar_create" in tools
        assert "drive_read" in tools
        assert "tasks_list" in tools

    def test_connector_validate_scope(self, connector):
        ok, _ = connector.validate_scope("gmail_search", GOOGLE_SCOPES["gmail_search"])
        assert ok is True
        ok2, _ = connector.validate_scope("gmail_send", GOOGLE_SCOPES["gmail_read"])
        assert ok2 is False


# ---------------------------------------------------------------------------
# Additional: encrypted no plaintext, owner isolation end-to-end
# ---------------------------------------------------------------------------
class TestVaultEncryptedNoPlaintext:
    @pytest.mark.asyncio
    async def test_vault_encrypted_storage(self, vault):
        ref = await vault.store("employee:kim", "google", GOOGLE_SCOPES["gmail_read"], b"secret-token::refresh")
        # internal store should be encrypted bytes, not plaintext
        stored = vault._store.get(ref)
        assert stored is not None
        assert b"secret-token" not in stored  # encrypted
        # but retrieve decrypts correctly
        plain = await vault.retrieve(ref, "agent:assistant:kim")
        assert plain == b"secret-token::refresh"

    @pytest.mark.asyncio
    async def test_owner_isolation_end_to_end_kim_lee(self, adapter, vault):
        # kim creates delegation and stores token
        d_kim = adapter.delegation_service.grant("employee:kim", "agent:assistant:kim", "google", GOOGLE_SCOPES["gmail_read"])
        ref_kim = await vault.store("employee:kim", "google", GOOGLE_SCOPES["gmail_read"], b"kim-token::kim-refresh")
        adapter.vault = vault
        adapter._binding[d_kim.id] = ref_kim
        # lee creates delegation
        d_lee = adapter.delegation_service.grant("employee:lee", "agent:assistant:lee", "google", GOOGLE_SCOPES["gmail_read"])
        ref_lee = await vault.store("employee:lee", "google", GOOGLE_SCOPES["gmail_read"], b"lee-token::lee-refresh")
        adapter._binding[d_lee.id] = ref_lee
        # kim can use kim's token via kim's resource
        res = await adapter.call_tool("gmail_search", {"q": "inbox", "resource": "gmail/user/kim/messages"}, _ctx("employee:kim", "agent:assistant:kim", d_kim.id), access_token="kim-token")
        assert res["tool"] == "gmail_search"
        # kim cannot use lee's token via lee's resource — owner isolation DENY before even checking token
        with pytest.raises(PermissionError, match="owner mismatch"):
            await adapter.call_tool("gmail_search", {"q": "inbox", "resource": "gmail/user/lee/messages"}, _ctx("employee:kim", "agent:assistant:kim", d_lee.id), access_token="lee-token")
        # kim cannot retrieve lee's secret_ref directly
        with pytest.raises(PermissionError):
            await adapter.get_access_token(ref_lee, "agent:assistant:kim")
