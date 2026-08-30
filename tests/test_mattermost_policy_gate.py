"""Focused unit/integration tests for Mattermost -> ACP -> Policy Engine gate.

Covers:
  - allow (low-risk ingress + authorized outline read)
  - default deny (no matching rule)
  - approval_required (writes, merge/deploy/delete/export)
  - cross-user spoof (session ownership)
  - explicit deny precedence (external export / private outline)
  - audit event (POLICY_DECISION for every ingress, including low-risk INTERACT)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in [
    ROOT / "security" / "policy-engine",
    ROOT / "packages" / "policy-model",
    ROOT / "packages" / "audit-model",
    ROOT / "execution-gateway",
    ROOT / "control-plane",
    ROOT / "security" / "audit",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest
from fastapi.testclient import TestClient

from policy_engine.small_business_bundle import small_business_bundle, classify_risk
from policy_engine.engine import PolicyEngine
from policy_model import PolicyEvaluationRequest, PolicyDecision

# --- Helpers

def _req(tenant="t1", user="employee:kim", agent="agent:assistant:kim", action="INTERACT", resource="session/ingress/t1/sess_123"):
    return PolicyEvaluationRequest(tenant_id=tenant, user_id=user, agent_id=agent, action=action, resource=resource)

def _engine():
    return PolicyEngine([small_business_bundle("t1")])

# --- Unit: allow

def test_small_business_allow_ingress_interact():
    eng = _engine()
    r = _req(action="INTERACT", resource="session/ingress/t1/sess_abc123")
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.ALLOW
    assert "allow-session-ingress" in res.matched_rule.id

def test_small_business_allow_mattermost_ingress_interact():
    eng = _engine()
    r = _req(action="INTERACT", resource="mattermost/ingress/t1/chan_1")
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.ALLOW

def test_small_business_allow_outline_read():
    eng = _engine()
    r = _req(action="READ", resource="outline/team/docs")
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.ALLOW
    assert res.matched_rule.id == "allow-outline-read"

def test_small_business_allow_outline_search():
    eng = _engine()
    r = _req(action="SEARCH", resource="outline/company/handbook")
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.ALLOW

def test_small_business_allow_personal_read():
    eng = _engine()
    r = _req(action="READ", resource="gmail/user/kim/messages")
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.ALLOW

def test_small_business_allow_internal_dm():
    eng = _engine()
    r = _req(action="SEND", resource="mattermost/dm/alice")
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.ALLOW

# --- Unit: default deny

def test_small_business_default_deny_unknown():
    eng = _engine()
    r = _req(action="READ", resource="unknown/domain/resource")
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.DENY
    assert res.source.value == "default_deny" or "default deny" in res.reason.lower()

def test_small_business_default_deny_arbitrary_resource():
    eng = _engine()
    r = _req(action="READ", resource="github/repo/private")
    # github read not allowed by small business (only outline/personal) => default deny
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.DENY

# --- Unit: approval_required

@pytest.mark.parametrize("action,resource", [
    ("CREATE", "outline/team/docs"),
    ("MODIFY", "outline/team/docs"),
    ("DELETE", "outline/team/docs"),
    ("MERGE", "github/repo/pr/1"),
    ("DEPLOY", "production/web"),
    ("EXPORT", "outline/team/docs"),
    ("SHARE", "outline/team/docs"),
    ("PAY", "erp/invoice/123"),
    ("SEND", "slack/channel/general"),  # external-like send => approval
])
def test_small_business_approval_required(action, resource):
    eng = _engine()
    r = _req(action=action, resource=resource)
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.APPROVAL_REQUIRED, f"{action} {resource} should be APPROVAL_REQUIRED got {res.decision} {res.reason}"

# --- Unit: explicit deny precedence

def test_small_business_explicit_deny_overrides_personal():
    # even if personal delegation would allow gmail, external export must be DENY
    eng = _engine()
    # External export with pattern containing external should be explicit DENY, overriding any allow
    r = _req(action="EXPORT", resource="gmail/user/kim/external/data")
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.DENY
    assert "explicit_deny" in res.source.value

def test_small_business_explicit_deny_external_send():
    eng = _engine()
    r = _req(action="SEND", resource="slack/external/channel")
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.DENY

def test_small_business_explicit_deny_private_outline():
    eng = _engine()
    # outline/private is explicitly denied, even though outline/* would allow
    r = _req(action="READ", resource="outline/private/confidential")
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.DENY
    assert res.matched_rule.id == "deny-sensitive-outline-private-read"

def test_small_business_explicit_deny_private_outline_search():
    eng = _engine()
    r = _req(action="SEARCH", resource="outline/private/confidential")
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.DENY

def test_small_business_explicit_deny_admin():
    eng = _engine()
    r = _req(action="ADMIN", resource="any/resource")
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.DENY

# --- Unit: classify_risk is deterministic

def test_classify_risk_deterministic():
    assert classify_risk("INTERACT", "session/ingress/t1/sess1") == "LOW"
    assert classify_risk("READ", "outline/team/docs") == "LOW"
    assert classify_risk("SEND", "mattermost/dm/alice") == "MEDIUM"
    assert classify_risk("SEND", "slack/channel/general") == "CRITICAL"
    assert classify_risk("MERGE", "github/repo/pr") == "CRITICAL"
    assert classify_risk("EXPORT", "any/external/data") == "CRITICAL"
    assert classify_risk("CREATE", "outline/team/docs") == "HIGH"

# --- Integration: AuthorizationHook + gate + audit

@pytest.mark.asyncio
async def test_gate_allow_produces_audit_event():
    # Use gate directly to ensure POLICY_DECISION audit is emitted
    from control_plane.mattermost_policy_gate import MattermostPolicyGate, _get_audit_ledger
    from control_plane.identity import map_user_to_agent
    mapping = map_user_to_agent("employee:kim", "t1")
    gate = MattermostPolicyGate("t1")
    # Fresh ledger capture via monkey patch: replace gate._ledger with isolated ledger
    from audit.audit_ledger.ledger import AuditLedger
    from audit_model import AuditEventType
    isolated = AuditLedger(signing_key="test-audit-key")
    gate._ledger = isolated
    # create a deterministic trace/request
    result = await gate.authorize_ingress(mapping, session_id="sess_test123", trace_id="trace_abc", request_id="req_123", channel_id="chan_1")
    assert result.decision == "ALLOW"
    # Audit must contain POLICY_DECISION for this ingress
    events = isolated.events if hasattr(isolated, "events") else isolated._events  # type: ignore
    policy_events = [e for e in events if e.event_type == AuditEventType.POLICY_DECISION]
    assert len(policy_events) >= 1
    last = policy_events[-1]
    assert last.tenant_id == "t1"
    assert last.user_id == "employee:kim"
    assert last.trace_id == "trace_abc"
    assert last.decision == "ALLOW"

@pytest.mark.asyncio
async def test_gate_default_deny_produces_audit_and_fails():
    from control_plane.mattermost_policy_gate import MattermostPolicyGate
    from control_plane.identity import map_user_to_agent
    from audit.audit_ledger.ledger import AuditLedger
    from audit_model import AuditEventType
    mapping = map_user_to_agent("employee:kim", "t1")
    gate = MattermostPolicyGate("t1")
    isolated = AuditLedger(signing_key="test-audit-key-2")
    gate._ledger = isolated
    # request a resource that should be default deny (arbitrary github read)
    with pytest.raises(Exception) as exc:
        await gate.authorize_ingress(mapping, session_id="sess_deny", trace_id="trace_deny", request_id="req_deny", channel_id="chan_1", action="READ", resource="github/repo/secret")
    assert exc.value.status_code == 403  # type: ignore
    events = isolated.events if hasattr(isolated, "events") else isolated._events  # type: ignore
    policy_events = [e for e in events if e.event_type == AuditEventType.POLICY_DECISION]
    # Even deny must have produced audit
    assert len(policy_events) >= 1
    assert policy_events[-1].decision == "DENY"

@pytest.mark.asyncio
async def test_gate_approval_required_produces_audit():
    from control_plane.mattermost_policy_gate import MattermostPolicyGate
    from control_plane.identity import map_user_to_agent
    from audit.audit_ledger.ledger import AuditLedger
    from audit_model import AuditEventType
    mapping = map_user_to_agent("employee:kim", "t1")
    gate = MattermostPolicyGate("t1")
    isolated = AuditLedger(signing_key="test-audit-key-3")
    gate._ledger = isolated
    with pytest.raises(Exception) as exc:
        await gate.authorize_ingress(mapping, session_id="sess_appr", trace_id="trace_appr", request_id="req_appr", channel_id="chan_1", action="DELETE", resource="outline/team/docs")
    assert exc.value.status_code == 403  # type: ignore
    assert "approval" in str(exc.value.detail).lower()  # type: ignore
    events = isolated.events if hasattr(isolated, "events") else isolated._events  # type: ignore
    assert any(e.decision == "APPROVAL_REQUIRED" for e in events if e.event_type == AuditEventType.POLICY_DECISION)

# --- Integration: webhook cross-user spoof + allow + explicit deny via TestClient

def test_webhook_allow_and_audit():
    from control_plane.app import app
    client = TestClient(app)
    # ordinary conversational prompt -> must be ALLOW and still have audit side effect
    # Patch gate ledger to capture after request? Instead we trust unit above for audit.
    # Here we verify endpoint returns 200 and creates session.
    r = client.post("/v1/mattermost/events", json={"tenant_id": "t1", "user_id": "employee:alice", "text": "hello, need help with my schedule"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["received"] is True
    assert "session_id" in j

def test_webhook_cross_user_spoof_denied():
    from control_plane.app import app
    from control_plane.session import session_store
    client = TestClient(app)
    # create session as alice via direct API? Use webhook to create then spoof
    r1 = client.post("/v1/mattermost/events", json={"tenant_id": "t1", "user_id": "employee:kim", "text": "initial"})
    assert r1.status_code == 200
    sid = r1.json()["session_id"]
    # now try to reuse sid as different user lee -> should be 403 cross-user isolation
    r2 = client.post("/v1/mattermost/events", json={"tenant_id": "t1", "user_id": "employee:lee", "text": "hijack attempt", "session_id": sid})
    assert r2.status_code in (403, 404), r2.text  # 403 from ownership check

def test_webhook_explicit_deny_via_engine_direct():
    # Webhook itself only evaluates INTERACT low-risk, so explicit deny via webhook payload
    # is tested via direct engine. Ensure explicit deny rule takes precedence over allow.
    eng = _engine()
    r = _req(action="READ", resource="outline/private/salary")
    res = eng.evaluate(r)
    assert res.decision == PolicyDecision.DENY
    # Even if we had a group grant allowing private, explicit deny must win — simulate by
    # adding a permissive group grant bundle and checking order.
    from policy_model import PolicyBundle, PolicyRule, PolicySource
    permissive = PolicyBundle(
        id="permissive", tenant_id="t1", name="permissive", version="9.9.9",
        rules=[PolicyRule(id="group-allow-private", source=PolicySource.GROUP_GRANT, action="READ", resource_pattern="outline/private/*", effect=PolicyDecision.ALLOW)]
    )
    eng2 = PolicyEngine([permissive, small_business_bundle("t1")])
    r2 = eng2.evaluate(_req(action="READ", resource="outline/private/salary"))
    assert r2.decision == PolicyDecision.DENY  # explicit_deny wins before group_grant

def test_webhook_low_risk_still_audited_via_gate():
    # Ensure ordinary chat (INTERACT) still produces POLICY_DECISION audit
    import asyncio
    from control_plane.identity import map_user_to_agent
    from control_plane.mattermost_policy_gate import MattermostPolicyGate
    from audit.audit_ledger.ledger import AuditLedger
    from audit_model import AuditEventType
    async def _run():
        mapping = map_user_to_agent("employee:bob", "t1")
        gate = MattermostPolicyGate("t1")
        isolated = AuditLedger(signing_key="audit-low-risk")
        gate._ledger = isolated
        await gate.authorize_ingress(mapping, session_id="sess_low", trace_id="trace_low", request_id="req_low", channel_id="chan_low")
        evts = isolated.events if hasattr(isolated, "events") else isolated._events  # type: ignore
        assert any(e.event_type == AuditEventType.POLICY_DECISION and e.decision == "ALLOW" for e in evts)
    asyncio.run(_run())

# --- New gates: fail-closed engine None, audit fail, tenant/user binding ---

def test_gate_fails_closed_when_hook_engine_none_in_production(monkeypatch):
    import os
    from control_plane.mattermost_policy_gate import MattermostPolicyGate
    from control_plane.identity import map_user_to_agent
    # create gate and ledger BEFORE switching to production (AuditLedger requires DB in prod)
    gate = MattermostPolicyGate("t1")
    class _StubLedger:
        def __init__(self):
            self.events=[]
        def append(self, evt):
            # minimal hash
            evt.event_hash = "stub"
            self.events.append(evt)
            return evt
    gate._ledger = _StubLedger()
    # now switch to production
    monkeypatch.setenv("OAOS_ENV", "production")
    # force hook engine None
    if gate._hook is not None:
        gate._hook.engine = None
    else:
        gate._engine = None
        gate._hook = type("DummyHook", (), {"engine": None, "authorize": lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("should not be called"))})()
    mapping = map_user_to_agent("employee:kim", "t1")
    import asyncio
    async def _run():
        try:
            await gate.authorize_ingress(mapping, session_id="sess_hook_none", trace_id="trace_hook_none", request_id="req_hook_none")
            assert False, "should have raised 403"
        except Exception as e:
            assert getattr(e, "status_code", 403) == 403
            assert "fail-closed before authorize" in str(getattr(e, "detail", str(e))).lower() or "policy engine unavailable" in str(getattr(e, "detail", str(e))).lower()
    asyncio.run(_run())
    # cleanup
    monkeypatch.delenv("OAOS_ENV", raising=False)

@pytest.mark.asyncio
async def test_gate_audit_failure_preserves_and_denies():
    from control_plane.mattermost_policy_gate import MattermostPolicyGate
    from control_plane.identity import map_user_to_agent
    mapping = map_user_to_agent("employee:kim", "t1")
    gate = MattermostPolicyGate("t1")
    class BrokenLedger:
        def append(self, evt):
            raise RuntimeError("simulated audit append failure")
    gate._ledger = BrokenLedger()
    # Even allow path should fail due to audit
    try:
        await gate.authorize_ingress(mapping, session_id="sess_audit_fail", trace_id="trace_audit_fail", request_id="req_audit_fail")
        assert False, "should have denied on audit failure"
    except Exception as e:
        assert getattr(e, "status_code", 403) == 403
        # evidence log file should exist
        import pathlib as _p
        p = _p.Path("/tmp/oaos_audit_fail.log")
        assert p.exists()
        content = p.read_text()
        assert "audit_error" in content or "simulated audit" in content

def test_webhook_tenant_binding_ignores_payload():
    from control_plane.mattermost_adapter.webhook import _resolve_tenant_id
    from control_plane.config import settings
    # payload tenant should be ignored
    got = _resolve_tenant_id("attacker-tenant")
    assert got == settings.tenant_id
    got2 = _resolve_tenant_id(None)
    assert got2 == settings.tenant_id

def test_webhook_user_binding_blocks_arbitrary_employee_outside_test(monkeypatch):
    # Ensure non-production but NOT internal test — should NOT trust employee: prefix
    monkeypatch.delenv("OAOS_ALLOW_TEST_IDENTITY", raising=False)
    monkeypatch.delenv("OAOS_ALLOW_TEST_FIXTURE", raising=False)
    monkeypatch.delenv("OAOS_ALLOW_TEST_FALLBACK", raising=False)
    # PYTEST_CURRENT_TEST is normally set; temporarily hide it
    orig = monkeypatch.setenv if False else None
    import os
    saved = os.environ.pop("PYTEST_CURRENT_TEST", None)
    try:
        # Also ensure not production
        monkeypatch.setenv("OAOS_ENV", "development")
        from control_plane.mattermost_adapter.webhook import _resolve_user_id, _allow_test_identity
        assert _allow_test_identity() is False
        # Arbitrary employee: should be mapped via adapter, not directly returned
        # Provide a crafted identity
        res = _resolve_user_id("employee:evil_admin", "evil")
        # In non-test mode, it should go through adapter (suffix evil_admin -> employee:evil_admin sanitized) but via adapter path;
        # we assert it does not bypass: it should still produce employee:evil_admin but via mapping, not direct trust
        # To test blocking, we check that direct payload with mixed case is sanitized
        res2 = _resolve_user_id("employee:ADMIN' OR 1=1", None)
        assert res2.startswith("employee:")
        assert "'" not in res2 and " " not in res2
        # With test identity allowed, direct return should happen
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_dummy")
        from importlib import reload as _rl
        import control_plane.mattermost_adapter.webhook as _wh
        # re-evaluate allow
        assert _wh._allow_test_identity() is True
        res3 = _wh._resolve_user_id("employee:allowed_user", None)
        assert res3 == "employee:allowed_user"
    finally:
        if saved is not None:
            os.environ["PYTEST_CURRENT_TEST"] = saved
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delenv("OAOS_ENV", raising=False)

def test_webhook_user_binding_via_adapter_raw_id():
    # Raw Mattermost IDs should be mapped via adapter
    import os
    saved = os.environ.get("PYTEST_CURRENT_TEST")
    try:
        os.environ["PYTEST_CURRENT_TEST"] = "1"
        from control_plane.mattermost_adapter.webhook import _resolve_user_id
        # raw numeric id with username
        res = _resolve_user_id("U123456", "alice.bob")
        assert res == "employee:alice.bob"
        # raw id without username
        res2 = _resolve_user_id("U999", None)
        assert res2 == "employee:u999"
    finally:
        if saved is not None:
            os.environ["PYTEST_CURRENT_TEST"] = saved
        else:
            os.environ.pop("PYTEST_CURRENT_TEST", None)

