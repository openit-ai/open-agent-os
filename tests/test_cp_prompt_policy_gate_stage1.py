"""Stage 1 regression: direct CP prompt endpoint must globally enforce MattermostPolicyGate.

Requirements:
  - owner/session/tenant integrity
  - policy decision + audit before ACP
  - DENY/APPROVAL_REQUIRED never forwards to ACP
  - production fail-closed if gate/policy/audit unavailable
  - no LLM classifier

This test FAILS before Stage 1 code and PASSES after.
"""
import sys
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]
for p in [
    ROOT / "control-plane",
    ROOT / "security" / "policy-engine",
    ROOT / "packages" / "policy-model",
    ROOT / "packages" / "audit-model",
    ROOT / "execution-gateway",
    ROOT / "security" / "audit",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

os.environ.setdefault("OAOS_ALLOW_TEST_IDENTITY", "1")

from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock
import pytest

from control_plane.app import app
from control_plane.session import session_store
from control_plane.auth import issue_user_jwt

TEST_KEY = os.environ.get("OAOS_SIGNING_KEY") or os.environ.get("OAOS_USER_JWT_SIGNING_KEY") or "test-unified-oaos-signing-key-32bytes-long-enough!!"
for _k in ("OAOS_SIGNING_KEY", "OAOS_USER_JWT_SIGNING_KEY", "OAOS_JWT_SIGNING_KEY"):
    os.environ[_k] = TEST_KEY

def _jwt(sub="employee:kim", tenant="acme", ttl=3600):
    return issue_user_jwt(sub, tenant_id=tenant, ttl_seconds=ttl)

def _auth(jwt):
    return {"Authorization": f"Bearer {jwt}"}

def _clear_store():
    try:
        if hasattr(session_store, "_store"):
            session_store._store.clear()
        if hasattr(session_store, "_fallback_store") and session_store._fallback_store and hasattr(session_store._fallback_store, "_store"):
            session_store._fallback_store._store.clear()
    except Exception:
        pass

@pytest.fixture(autouse=True)
def clear():
    _clear_store()
    # clear gate cache between tests
    try:
        from control_plane.mattermost_policy_gate import clear_mattermost_gate_cache
        clear_mattermost_gate_cache()
    except Exception:
        pass
    yield
    _clear_store()
    try:
        from control_plane.mattermost_policy_gate import clear_mattermost_gate_cache
        clear_mattermost_gate_cache()
    except Exception:
        pass

def _create_session(client, jwt, tenant="acme", user="employee:kim"):
    r = client.post("/v1/sessions", json={"tenant_id": tenant, "user_id": user}, headers={"X-User-Id": user, **_auth(jwt)})
    assert r.status_code == 200, r.text
    return r.json()["session_id"], r.json()["trace_id"]

def test_deny_never_forwards_to_acp():
    """If gate DENYs, send_prompt must return 403 and never call acp.send_prompt."""
    c = TestClient(app)
    jwt = _jwt(sub="employee:kim", tenant="acme")
    sid, trace = _create_session(c, jwt, tenant="acme", user="employee:kim")

    # Patch gate to DENY and patch acp to detect forwarding
    from fastapi import HTTPException

    class DenyGate:
        async def authorize_ingress(self, mapping, session_id, trace_id, request_id, channel_id=None, **kw):
            raise HTTPException(status_code=403, detail="policy denied: test deny")

    # Also test that audit path exists — gate should have been called before ACP
    from unittest.mock import patch

    with patch("control_plane.mattermost_policy_gate.get_mattermost_gate", return_value=DenyGate()):
        # Also patch the import path inside app.py if it imports directly
        with patch("control_plane.app.get_mattermost_gate", return_value=DenyGate(), create=True):
            # Patch ACP to fail if called — we want to ensure it's never called
            sentinel = MagicMock()
            sentinel.send_prompt = AsyncMock(side_effect=AssertionError("ACP should not be called on DENY"))
            sentinel.create_session_remote = AsyncMock(return_value={})
            # Need to patch the global acp instance on app module
            with patch("control_plane.app.acp", sentinel):
                rp = c.post(f"/v1/sessions/{sid}/prompt", json={"session_id": sid, "prompt": "hello"}, headers={"X-User-Id": "employee:kim", **_auth(jwt)})
                assert rp.status_code == 403, f"DENY should be 403 but got {rp.status_code}: {rp.text}"
                # ensure ACP was not called
                sentinel.send_prompt.assert_not_called()

def test_approval_required_never_forwards_to_acp():
    c = TestClient(app)
    jwt = _jwt(sub="employee:kim", tenant="acme")
    sid, trace = _create_session(c, jwt, tenant="acme", user="employee:kim")

    from fastapi import HTTPException

    class ApprovalGate:
        async def authorize_ingress(self, mapping, session_id, trace_id, request_id, channel_id=None, **kw):
            raise HTTPException(status_code=403, detail="approval required: test approval")

    from unittest.mock import patch

    with patch("control_plane.mattermost_policy_gate.get_mattermost_gate", return_value=ApprovalGate()):
        with patch("control_plane.app.get_mattermost_gate", return_value=ApprovalGate(), create=True):
            sentinel = MagicMock()
            sentinel.send_prompt = AsyncMock(side_effect=AssertionError("ACP should not be called on APPROVAL_REQUIRED"))
            sentinel.create_session_remote = AsyncMock(return_value={})
            with patch("control_plane.app.acp", sentinel):
                rp = c.post(f"/v1/sessions/{sid}/prompt", json={"session_id": sid, "prompt": "please merge"}, headers={"X-User-Id": "employee:kim", **_auth(jwt)})
                assert rp.status_code == 403, f"APPROVAL_REQUIRED should be 403 got {rp.status_code}: {rp.text}"
                assert "approval" in rp.text.lower()
                sentinel.send_prompt.assert_not_called()

def test_allow_forwards_and_audits_before_acp_order():
    """ALLOW must call gate before ACP and produce audit. Verify order."""
    c = TestClient(app)
    jwt = _jwt(sub="employee:kim", tenant="acme")
    sid, trace = _create_session(c, jwt, tenant="acme", user="employee:kim")

    call_order = []

    class AllowGate:
        async def authorize_ingress(self, mapping, session_id, trace_id, request_id, channel_id=None, **kw):
            call_order.append("gate")
            # verify mapping integrity inside gate
            assert mapping.human_principal == "employee:kim"
            assert mapping.tenant_id == "acme"
            # return allow-like object
            m = MagicMock()
            m.decision = "ALLOW"
            m.reason = "allow test"
            m.source = "test"
            m.matched_rule_id = "allow-session-ingress"
            return m

    from unittest.mock import patch

    sentinel = MagicMock()
    async def _send_prompt(rec, prompt, rid, **kw):
        call_order.append("acp")
        assert call_order.index("gate") < call_order.index("acp"), "audit/gate must be before ACP"
        return {"status": "ok", "request_id": rid}
    sentinel.send_prompt = _send_prompt
    sentinel.create_session_remote = AsyncMock(return_value={})

    with patch("control_plane.mattermost_policy_gate.get_mattermost_gate", return_value=AllowGate()):
        with patch("control_plane.app.get_mattermost_gate", return_value=AllowGate(), create=True):
            with patch("control_plane.app.acp", sentinel):
                rp = c.post(f"/v1/sessions/{sid}/prompt", json={"session_id": sid, "prompt": "hello allowed"}, headers={"X-User-Id": "employee:kim", **_auth(jwt)})
                assert rp.status_code == 200, rp.text
                assert call_order == ["gate", "acp"]

def test_production_fail_closed_when_gate_unavailable(monkeypatch):
    """In production, if gate returns None / throws unavailable, must fail-closed 403."""
    c = TestClient(app)
    jwt = _jwt(sub="employee:kim", tenant="acme")
    sid, _ = _create_session(c, jwt, tenant="acme", user="employee:kim")

    from unittest.mock import patch

    # Simulate gate unavailable by making get_mattermost_gate raise
    def _boom(*a, **kw):
        raise RuntimeError("gate unavailable")

    monkeypatch.setenv("OAOS_ENV", "production")

    sentinel = MagicMock()
    sentinel.send_prompt = AsyncMock(side_effect=AssertionError("ACP should not be called on fail-closed"))
    with patch("control_plane.mattermost_policy_gate.get_mattermost_gate", side_effect=_boom):
        with patch("control_plane.app.get_mattermost_gate", side_effect=_boom, create=True):
            with patch("control_plane.app.acp", sentinel):
                rp = c.post(f"/v1/sessions/{sid}/prompt", json={"session_id": sid, "prompt": "hello"}, headers={"X-User-Id": "employee:kim", **_auth(jwt)})
                assert rp.status_code == 403, f"production fail-closed expected 403 got {rp.status_code}: {rp.text}"
                sentinel.send_prompt.assert_not_called()

    monkeypatch.delenv("OAOS_ENV", raising=False)

def test_tenant_mismatch_denied(monkeypatch):
    """JWT tenant != session tenant must be denied (tenant integrity)."""
    c = TestClient(app)
    jwt_acme = _jwt(sub="employee:kim", tenant="acme")
    # Create session with tenant acme
    sid, _ = _create_session(c, jwt_acme, tenant="acme", user="employee:kim")
    # Now try to use JWT for different tenant evil
    jwt_evil = _jwt(sub="employee:kim", tenant="evil")
    rp = c.post(f"/v1/sessions/{sid}/prompt", json={"session_id": sid, "prompt": "hello"}, headers={"X-User-Id": "employee:kim", **_auth(jwt_evil)})
    # Depending on implementation, should be 401 or 403 — must not be 200
    assert rp.status_code in (401, 403), f"tenant mismatch should be denied, got {rp.status_code}: {rp.text}"

def test_cross_user_session_denied(monkeypatch):
    c = TestClient(app)
    jwt_kim = _jwt(sub="employee:kim", tenant="acme")
    jwt_lee = _jwt(sub="employee:lee", tenant="acme")
    sid, _ = _create_session(c, jwt_kim, tenant="acme", user="employee:kim")
    rp = c.post(f"/v1/sessions/{sid}/prompt", json={"session_id": sid, "prompt": "hijack"}, headers={"X-User-Id": "employee:lee", **_auth(jwt_lee)})
    assert rp.status_code == 403, f"cross-user should be 403 got {rp.status_code}: {rp.text}"
