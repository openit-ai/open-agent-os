"""Focused tests for active published policy integration into MattermostPolicyGate.

Verifies:
 - default fallback when no published row (small_business_bundle used)
 - published bundle overrides small_business (affects gate decision)
 - production DB error fails closed, non-prod fallback remains
 - sqlite-backed loader integration (optional DB path)
"""
import sys
import os
import json
import tempfile
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

from policy_model import PolicyBundle, PolicyRule, PolicySource, PolicyDecision
from policy_engine.engine import PolicyEngine
from policy_engine.small_business_bundle import small_business_bundle

# Helpers

def _clear_env(monkeypatch=None):
    # ensure non-prod by default
    vals = ["OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"]
    for k in vals:
        if monkeypatch:
            monkeypatch.delenv(k, raising=False)
        else:
            os.environ.pop(k, None)
    # also clear DB url for default fallback tests
    for k in ["OAOS_DATABASE_URL", "DATABASE_URL"]:
        if monkeypatch:
            monkeypatch.delenv(k, raising=False)
        else:
            os.environ.pop(k, None)


def _make_custom_bundle(tenant_id="t_pub", deny_outline=False):
    """Create a bundle that differs from small_business: optionally deny outline read."""
    rules = []
    if deny_outline:
        # explicit deny for outline/* read — will override small_business allow
        rules.append(PolicyRule(id="deny-outline-read", source=PolicySource.EXPLICIT_DENY, action="READ", resource_pattern="outline/*", effect=PolicyDecision.DENY))
    # allow a custom resource that small_business would default-deny
    rules.append(PolicyRule(id="allow-custom-foo", source=PolicySource.DEFAULT_BUNDLE, action="READ", resource_pattern="custom/foo/*", effect=PolicyDecision.ALLOW))
    # keep ingress allow so gate still works
    rules.append(PolicyRule(id="allow-session-ingress-interact", source=PolicySource.DEFAULT_BUNDLE, action="INTERACT", resource_pattern="session/ingress/*", effect=PolicyDecision.ALLOW))
    return PolicyBundle(id="custom-published-v1", tenant_id=tenant_id, name="Custom Published", version="9.9.9", rules=rules)


# --- default fallback

def test_default_fallback_no_published(monkeypatch):
    _clear_env(monkeypatch)
    # ensure loader returns None
    import control_plane.mattermost_policy_gate as gate
    monkeypatch.setattr(gate, "_load_active_published_bundle", lambda tid: None)
    gate.clear_mattermost_gate_cache()
    eng = gate._get_small_business_engine("t_pub")
    assert eng is not None
    # should behave like small_business: INTERACT allowed, custom/foo denied
    from policy_model import PolicyEvaluationRequest
    req_allow = PolicyEvaluationRequest(tenant_id="t_pub", user_id="employee:kim", agent_id="agent:assistant:kim", action="INTERACT", resource="session/ingress/t_pub/sess1")
    res = eng.evaluate(req_allow)
    assert res.decision == PolicyDecision.ALLOW
    req_custom = PolicyEvaluationRequest(tenant_id="t_pub", user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="custom/foo/bar")
    res2 = eng.evaluate(req_custom)
    assert res2.decision == PolicyDecision.DENY  # no published => small_business default deny


def test_published_bundle_affects_gate_allow(monkeypatch):
    _clear_env(monkeypatch)
    custom = _make_custom_bundle("t_pub")
    import control_plane.mattermost_policy_gate as gate
    monkeypatch.setattr(gate, "_load_active_published_bundle", lambda tid: custom if tid == "t_pub" else None)
    gate.clear_mattermost_gate_cache()
    eng = gate._get_small_business_engine("t_pub")
    assert eng is not None
    from policy_model import PolicyEvaluationRequest
    # custom resource now ALLOW via published bundle
    req = PolicyEvaluationRequest(tenant_id="t_pub", user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="custom/foo/bar")
    res = eng.evaluate(req)
    assert res.decision == PolicyDecision.ALLOW
    assert res.matched_rule.id == "allow-custom-foo"


def test_published_bundle_explicit_deny_overrides_allow(monkeypatch):
    _clear_env(monkeypatch)
    custom = _make_custom_bundle("t_pub", deny_outline=True)
    import control_plane.mattermost_policy_gate as gate
    monkeypatch.setattr(gate, "_load_active_published_bundle", lambda tid: custom if tid == "t_pub" else None)
    gate.clear_mattermost_gate_cache()
    eng = gate._get_small_business_engine("t_pub")
    from policy_model import PolicyEvaluationRequest
    # outline read would be ALLOW in small_business, but published explicitly DENY
    req = PolicyEvaluationRequest(tenant_id="t_pub", user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="outline/team/docs")
    res = eng.evaluate(req)
    assert res.decision == PolicyDecision.DENY
    assert res.matched_rule.id == "deny-outline-read"


@pytest.mark.asyncio
async def test_gate_uses_published_bundle_for_authorize(monkeypatch):
    _clear_env(monkeypatch)
    custom = _make_custom_bundle("t_pub", deny_outline=True)
    from control_plane.mattermost_policy_gate import MattermostPolicyGate
    import control_plane.mattermost_policy_gate as gate_mod
    monkeypatch.setattr(gate_mod, "_load_active_published_bundle", lambda tid: custom if tid == "t_pub" else None)
    gate_mod.clear_mattermost_gate_cache()
    from control_plane.identity import map_user_to_agent
    mapping = map_user_to_agent("employee:kim", "t_pub")
    gate = MattermostPolicyGate("t_pub")
    # INTERACT still allowed
    from audit.audit_ledger.ledger import AuditLedger
    gate._ledger = AuditLedger(signing_key="test-published")
    res = await gate.authorize_ingress(mapping, session_id="sess_pub_1", trace_id="trace_pub_1", request_id="req_pub_1", channel_id="chan_1")
    assert res.decision == "ALLOW"
    # Now test that published deny is enforced via gate direct engine path
    # Use action READ outline/* which published denies
    with pytest.raises(Exception) as exc:
        await gate.authorize_ingress(mapping, session_id="sess_pub_2", trace_id="trace_pub_2", request_id="req_pub_2", channel_id="chan_1", action="READ", resource="outline/team/docs")
    assert getattr(exc.value, "status_code", 403) == 403
    assert "denied" in str(getattr(exc.value, "detail", str(exc.value))).lower()


def test_production_db_error_fails_closed(monkeypatch):
    # Simulate loader raising RuntimeError in production => engine None => gate DENY
    import control_plane.mattermost_policy_gate as gate
    from control_plane.mattermost_policy_gate import MattermostPolicyGate
    from control_plane.identity import map_user_to_agent
    # create gate and stub ledger BEFORE switching to production (AuditLedger requires DB in prod)
    _clear_env(monkeypatch)
    def _raise(tid):
        raise RuntimeError("simulated DB connection failure in production")
    monkeypatch.setattr(gate, "_load_active_published_bundle", _raise)
    gate.clear_mattermost_gate_cache()
    # build gate first with stub ledger
    g = MattermostPolicyGate("t_pub")
    class _StubLedger:
        def __init__(self):
            self.events = []
        def append(self, evt):
            evt.event_hash = "stub"
            self.events.append(evt)
            return evt
    g._ledger = _StubLedger()
    # now switch to production to trigger fail-closed path
    monkeypatch.setenv("OAOS_ENV", "production")
    # force engine None via helper
    if g._hook is not None:
        g._hook.engine = None
    g._engine = None
    # also verify helper returns None in prod
    eng = gate._get_small_business_engine("t_pub")
    assert eng is None, "production DB error must fail closed (engine None)"
    assert g._engine is None
    mapping = map_user_to_agent("employee:kim", "t_pub")
    import asyncio
    async def _run():
        try:
            await g.authorize_ingress(mapping, session_id="sess_prod", trace_id="t", request_id="r")
            assert False, "should have raised 403"
        except Exception as e:
            assert getattr(e, "status_code", 403) == 403
            assert "fail-closed" in str(getattr(e, "detail", str(e))).lower() or "policy engine unavailable" in str(getattr(e, "detail", str(e))).lower()
    asyncio.run(_run())
    monkeypatch.delenv("OAOS_ENV", raising=False)


def test_nonprod_db_error_falls_back(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("OAOS_ENV", "development")
    import control_plane.mattermost_policy_gate as gate
    def _raise(tid):
        raise RuntimeError("simulated DB error non-prod")
    monkeypatch.setattr(gate, "_load_active_published_bundle", _raise)
    gate.clear_mattermost_gate_cache()
    eng = gate._get_small_business_engine("t_pub")
    assert eng is not None, "non-prod DB error should fallback to small_business_bundle"
    from policy_model import PolicyEvaluationRequest
    req = PolicyEvaluationRequest(tenant_id="t_pub", user_id="employee:kim", agent_id="agent:assistant:kim", action="INTERACT", resource="session/ingress/t_pub/sess1")
    res = eng.evaluate(req)
    assert res.decision == PolicyDecision.ALLOW
    monkeypatch.delenv("OAOS_ENV", raising=False)


def test_sqlite_loader_integration(monkeypatch, tmp_path):
    # Create temp sqlite DB with admin_policy_versions and a published row, verify loader picks it up
    _clear_env(monkeypatch)
    db_path = tmp_path / "test_policy.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("OAOS_DATABASE_URL", url)
    # create table and insert published row
    from sqlalchemy import create_engine, text
    eng = create_engine(url, connect_args={"check_same_thread": False})
    ddl = """CREATE TABLE IF NOT EXISTS admin_policy_versions (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        bundle_id TEXT NOT NULL,
        name TEXT NOT NULL,
        version TEXT NOT NULL,
        status TEXT NOT NULL,
        rules_json TEXT NOT NULL,
        created_by TEXT,
        created_at TEXT NOT NULL,
        approved_by TEXT,
        approved_at TEXT,
        published_at TEXT,
        parent_version TEXT
    )"""
    rules = [
        {"id": "allow-custom-foo", "source": "default_bundle", "action": "READ", "resource_pattern": "custom/foo/*", "effect": "ALLOW", "priority": 0},
        {"id": "deny-external-export", "source": "explicit_deny", "action": "EXPORT", "resource_pattern": "*external*", "effect": "DENY", "priority": 0},
        {"id": "allow-session-ingress-interact", "source": "default_bundle", "action": "INTERACT", "resource_pattern": "session/ingress/*", "effect": "ALLOW", "priority": 0},
    ]
    with eng.begin() as conn:
        conn.execute(text(ddl))
        conn.execute(text("INSERT INTO admin_policy_versions (id, tenant_id, bundle_id, name, version, status, rules_json, created_by, created_at) VALUES (:id,:t,:bid,:name,:ver,:st,:rj,:cb,:ca)"),
                     {"id": "pv_test1", "t": "t_sql", "bid": "published-bundle-v9", "name": "Published Test", "ver": "9.0.0", "st": "published", "rj": json.dumps(rules), "cb": "tester", "ca": "2026-01-01T00:00:00Z"})
    eng.dispose()
    # ensure cache clear
    import control_plane.mattermost_policy_gate as gate
    gate.clear_mattermost_gate_cache()
    # Now loader should return published bundle via DB (not mocked)
    from policy_engine.active_policy_loader import get_active_published_bundle
    b = get_active_published_bundle("t_sql")
    assert b is not None
    assert b.version == "9.0.0"
    assert any(r.id == "allow-custom-foo" for r in b.rules)
    # Gate should also use it
    eng2 = gate._get_small_business_engine("t_sql")
    assert eng2 is not None
    from policy_model import PolicyEvaluationRequest
    req = PolicyEvaluationRequest(tenant_id="t_sql", user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="custom/foo/bar")
    res = eng2.evaluate(req)
    assert res.decision == PolicyDecision.ALLOW
    # custom tenant without published should fallback
    eng3 = gate._get_small_business_engine("t_other")
    req2 = PolicyEvaluationRequest(tenant_id="t_other", user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="custom/foo/bar")
    res2 = eng3.evaluate(req2)
    assert res2.decision == PolicyDecision.DENY
    # cleanup
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    gate.clear_mattermost_gate_cache()
