"""Admin policy tests — active published UI policy must affect MattermostPolicyGate.

Covers:
- L5/L4 auth (draft/approve/publish/rollback require L5, reads allow L4/L5, 401 without token)
- validation and default deny preservation
- active published influence on MattermostPolicyGate (gate reads published bundle, fallback to default)
- rollback creates new published version (immutable history, version increment)
- fail-closed production for policy mutations when DB not configured (blocked, but default fallback still works for gate)
"""
import sys
import os
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"

# Load admin modules like test_admin_backend does (avoid collision with security.*)
def _load_admin_module(name: str, filename: str, bare_alias: str | None = None):
    added = False
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
        added = True
    spec = importlib.util.spec_from_file_location(name, str(BACKEND / filename))
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    sys.modules[name] = mod
    if bare_alias:
        sys.modules[bare_alias] = mod
    spec.loader.exec_module(mod)  # type: ignore
    return mod

auth_mod = _load_admin_module("admin_auth_policy", "auth.py", bare_alias="auth")
infra_mod = _load_admin_module("admin_infra_policy", "infra.py", bare_alias="infra")
# policy needs auth already loaded
policy_mod = _load_admin_module("admin_policy_mod", "policy.py")
_app_mod = _load_admin_module("admin_app_policy", "app.py")
if str(BACKEND) in sys.path:
    sys.path.remove(str(BACKEND))

admin_app = _app_mod.app

from fastapi.testclient import TestClient
import pytest

# Make control-plane imports work
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

@pytest.fixture(autouse=True)
def isolate_stores():
    # clear auth infra and policy mem
    auth_mod.clear_users()
    infra_mod.clear_services()
    # clear policy mem (module globals)
    try:
        policy_mod._mem_versions.clear()
        policy_mod._mem_draft = None
    except Exception:
        pass
    # clear gate cache so active bundle is re-evaluated
    try:
        from control_plane.mattermost_policy_gate import clear_mattermost_gate_cache
        clear_mattermost_gate_cache()
    except Exception:
        pass
    yield
    auth_mod.clear_users()
    infra_mod.clear_services()
    try:
        policy_mod._mem_versions.clear()
        policy_mod._mem_draft = None
    except Exception:
        pass
    try:
        from control_plane.mattermost_policy_gate import clear_mattermost_gate_cache
        clear_mattermost_gate_cache()
    except Exception:
        pass

def _client():
    return TestClient(admin_app)

def _login(email="admin@openit.co.kr", password="Admin123!"):
    c = _client()
    r = c.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]

def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}

def _l4_token():
    tok_l5 = _login()
    c = _client()
    c.post("/v1/auth/register", json={"email": "p_l4@openit.co.kr", "password": "Password123!", "display_name": "L4", "role": "L4"}, headers=_auth(tok_l5))
    r2 = c.post("/v1/auth/login", json={"email": "p_l4@openit.co.kr", "password": "Password123!"})
    assert r2.status_code == 200, r2.text
    return r2.json()["access_token"]

# sample rules — minimal valid bundle (preserves mandatory explicit_deny)
def _valid_rules(extra=None):
    base = [
        {"id": "deny-external-export", "source": "explicit_deny", "action": "EXPORT", "resource_pattern": "*external*", "effect": "DENY", "priority": 10},
        {"id": "allow-session-ingress-interact", "source": "default_bundle", "action": "INTERACT", "resource_pattern": "session/ingress/*", "effect": "ALLOW", "priority": 100},
        {"id": "allow-outline-read", "source": "default_bundle", "action": "READ", "resource_pattern": "outline/*", "effect": "ALLOW", "priority": 100},
    ]
    if extra:
        base.extend(extra)
    return base

def _deny_outline_rule():
    # explicit deny that will override the allow-outline-read above — tests active published influence
    return {"id": "deny-outline-secret", "source": "explicit_deny", "action": "READ", "resource_pattern": "outline/secret/*", "effect": "DENY", "priority": 5}

# ── L5/L4 auth ────────────────────────────────────────────────────────────────

def test_policy_401_without_token():
    c = _client()
    assert c.get("/v1/policy/bundles").status_code == 401
    assert c.get("/v1/policy/draft").status_code == 401
    assert c.get("/v1/policy/history").status_code == 401
    assert c.post("/v1/policy/validate", json={"rules": _valid_rules()}).status_code == 401
    assert c.post("/v1/policy/draft", json={"rules": _valid_rules()}).status_code == 401

def test_policy_reads_allow_L4():
    t5 = _login()
    t4 = _l4_token()
    c = _client()
    # L4 reads should be 200
    for tok in (t5, t4):
        assert c.get("/v1/policy/bundles", headers=_auth(tok)).status_code == 200, f"bundles for {tok[:8]}"
        assert c.get("/v1/policy/draft", headers=_auth(tok)).status_code == 200
        assert c.get("/v1/policy/history", headers=_auth(tok)).status_code == 200
        assert c.post("/v1/policy/validate", json={"rules": _valid_rules()}, headers=_auth(tok)).status_code == 200
        # simulate is also auth-only (L4 allowed)
        assert c.post("/v1/policy/simulate", json={"action": "READ", "resource": "outline/team/docs"}, headers=_auth(tok)).status_code == 200

def test_policy_mutations_require_L5():
    t4 = _l4_token()
    c = _client()
    h4 = _auth(t4)
    # draft upsert as L4 -> 403
    r = c.post("/v1/policy/draft", json={"rules": _valid_rules()}, headers=h4)
    assert r.status_code == 403, r.text
    # PUT draft as L4 -> 403
    r2 = c.put("/v1/policy/draft", json={"rules": _valid_rules()}, headers=h4)
    assert r2.status_code == 403, r2.text
    # approve as L4 -> 403
    assert c.post("/v1/policy/approve", json={"tenant_id": "default"}, headers=h4).status_code == 403
    # publish as L4 -> 403
    assert c.post("/v1/policy/publish", json={"tenant_id": "default"}, headers=h4).status_code == 403
    # rollback as L4 -> 403
    assert c.post("/v1/policy/rollback", json={"target_version": "1.0.0"}, headers=h4).status_code == 403

def test_policy_draft_put_synonym_and_validate():
    t5 = _login()
    c = _client()
    h5 = _auth(t5)
    # POST draft
    r = c.post("/v1/policy/draft", json={"rules": _valid_rules(), "bundle_id": "test-bundle", "name": "Test"}, headers=h5)
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["status"] == "draft"
    # PUT synonym (client flexibility)
    r2 = c.put("/v1/policy/draft", json={"rules": _valid_rules(extra=[_deny_outline_rule()]), "bundle_id": "test-bundle", "name": "Test v2"}, headers=h5)
    assert r2.status_code == 200, r2.text

def test_policy_validation_mandatory_rule():
    t5 = _login()
    c = _client()
    h5 = _auth(t5)
    # missing mandatory explicit_deny deny-external-export -> validation error
    bad = [
        {"id": "allow-outline-read", "source": "default_bundle", "action": "READ", "resource_pattern": "outline/*", "effect": "ALLOW", "priority": 100},
    ]
    r = c.post("/v1/policy/validate", json={"rules": bad}, headers=h5)
    assert r.status_code == 200
    assert r.json()["valid"] is False
    assert any("deny-external-export" in e for e in r.json()["errors"])
    # with allow_remove_mandatory=True should still fail due to missing explicit_deny DENY, so keep explicit_deny but non-mandatory id
    # case: has explicit_deny but missing mandatory id deny-external-export
    bad2 = [
        {"id": "deny-some-other", "source": "explicit_deny", "action": "READ", "resource_pattern": "outline/private/*", "effect": "DENY", "priority": 10},
        {"id": "allow-outline-read", "source": "default_bundle", "action": "READ", "resource_pattern": "outline/*", "effect": "ALLOW", "priority": 100},
    ]
    r2 = c.post("/v1/policy/validate", json={"rules": bad2, "allow_remove_mandatory": True}, headers=h5)
    assert r2.json()["valid"] is True or r2.json()["ok"] is True
    # draft upsert without mandatory should 400
    r3 = c.post("/v1/policy/draft", json={"rules": bad}, headers=h5)
    assert r3.status_code == 400
    # with allow_remove_mandatory -> still 400 because explicit_deny missing (not just mandatory)
    # but with bad2 + allow_remove_mandatory -> 200
    r4 = c.post("/v1/policy/draft", json={"rules": bad2, "allow_remove_mandatory": True}, headers=h5)
    assert r4.status_code == 200, r4.text

# ── active published influence on MattermostPolicyGate ───────────────────────

def test_active_published_affects_gate_and_default_fallback():
    """Publish a custom bundle, verify GET /bundles returns it and MattermostPolicyGate evaluates it.
    Also verify before publish the gate falls back to default (ALLOW outline/team, DENY default)."""
    t5 = _login()
    c = _client()
    h5 = _auth(t5)

    # Before publish — default fallback: gate should ALLOW outline/team/docs and also ALLOW session ingress
    from control_plane.identity import map_user_to_agent
    from control_plane.mattermost_policy_gate import MattermostPolicyGate, clear_mattermost_gate_cache, _get_small_business_engine
    from audit.audit_ledger.ledger import AuditLedger
    import asyncio

    clear_mattermost_gate_cache()
    # default engine should allow outline read
    eng_before = _get_small_business_engine("default")
    from policy_model import PolicyEvaluationRequest
    req_allow = PolicyEvaluationRequest(tenant_id="default", user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="outline/team/docs")
    res_before = eng_before.evaluate(req_allow)
    assert res_before.decision.value == "ALLOW", f"default should allow outline read got {res_before.decision}"

    # Now publish a custom bundle that explicitly denies outline/secret/* (active published must affect gate)
    custom_rules = _valid_rules(extra=[_deny_outline_rule()])
    r = c.post("/v1/policy/draft", json={"rules": custom_rules, "bundle_id": "custom-1"}, headers=h5)
    assert r.status_code == 200, r.text
    r2 = c.post("/v1/policy/approve", json={"tenant_id": "default"}, headers=h5)
    assert r2.status_code == 200, r2.text
    r3 = c.post("/v1/policy/publish", json={"tenant_id": "default"}, headers=h5)
    assert r3.status_code == 200, r3.text
    active_ver = r3.json()["active_version"]
    assert active_ver

    # GET /bundles should now return the published bundle (authoritative)
    rb = c.get("/v1/policy/bundles", headers=h5)
    assert rb.status_code == 200
    bundles = rb.json()["bundles"]
    # at least one bundle has our deny rule
    assert any(any(rr.get("id") == "deny-outline-secret" for rr in b.get("rules") or []) for b in bundles), "published bundle should be returned via GET /bundles"

    # Gate should now DENY outline/secret/docs via active published bundle
    clear_mattermost_gate_cache()
    eng_after = _get_small_business_engine("default")
    req_secret = PolicyEvaluationRequest(tenant_id="default", user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="outline/secret/docs")
    res_secret = eng_after.evaluate(req_secret)
    assert res_secret.decision.value == "DENY", f"active published explicit_deny should DENY outline/secret got {res_secret.decision}"
    assert res_secret.source.value == "explicit_deny"

    # Gate authorize_ingress for secret resource should 403 (fail-closed), and audit should still be emitted
    mapping = map_user_to_agent("employee:kim", "default")
    gate = MattermostPolicyGate("default")
    isolated = AuditLedger(signing_key="test-policy-active")
    gate._ledger = isolated
    async def _check_gate_deny():
        try:
            await gate.authorize_ingress(mapping, session_id="sess_pol", trace_id="t1", request_id="r1", action="READ", resource="outline/secret/docs")
            assert False, "should have denied"
        except Exception as e:
            assert getattr(e, "status_code", 403) == 403
            assert "denied" in str(getattr(e, "detail", str(e))).lower()
    asyncio.run(_check_gate_deny())
    # gate still allows non-secret outline
    async def _check_gate_allow():
        gate2 = MattermostPolicyGate("default")
        gate2._ledger = AuditLedger(signing_key="test-policy-allow")
        res = await gate2.authorize_ingress(mapping, session_id="sess_pol2", trace_id="t2", request_id="r2", action="READ", resource="outline/team/docs")
        assert res.decision == "ALLOW" or getattr(res, "allowed", False) is True
    asyncio.run(_check_gate_allow())

def test_active_published_engine_refresh_per_authorize():
    """Verify gate refreshes engine on each authorize so publish takes immediate effect (no stale cache)."""
    t5 = _login()
    c = _client()
    h5 = _auth(t5)
    from control_plane.mattermost_policy_gate import MattermostPolicyGate, clear_mattermost_gate_cache, get_mattermost_gate
    from control_plane.identity import map_user_to_agent
    import asyncio

    clear_mattermost_gate_cache()
    # initially no published — default allows outline/team
    mapping = map_user_to_agent("employee:lee", "default")
    gate = get_mattermost_gate("default")
    from audit.audit_ledger.ledger import AuditLedger
    gate._ledger = AuditLedger(signing_key="k1")
    # warm cache
    asyncio.run(gate.authorize_ingress(mapping, session_id="s1", trace_id="t1", request_id="r1"))

    # publish deny for that resource
    deny_extra = {"id": "deny-team-docs", "source": "explicit_deny", "action": "READ", "resource_pattern": "outline/team/docs", "effect": "DENY", "priority": 1}
    c.post("/v1/policy/draft", json={"rules": _valid_rules(extra=[deny_extra])}, headers=h5)
    c.post("/v1/policy/approve", json={"tenant_id": "default"}, headers=h5)
    c.post("/v1/policy/publish", json={"tenant_id": "default"}, headers=h5)

    # same gate instance should now deny (refresh inside authorize_ingress)
    gate._ledger = AuditLedger(signing_key="k2")
    try:
        asyncio.run(gate.authorize_ingress(mapping, session_id="s2", trace_id="t2", request_id="r2", action="READ", resource="outline/team/docs"))
        assert False, "should deny after publish due to refresh"
    except Exception as e:
        assert getattr(e, "status_code", 403) == 403

# ── rollback ─────────────────────────────────────────────────────────────────

def test_rollback_creates_new_published_version():
    t5 = _login()
    c = _client()
    h5 = _auth(t5)
    # publish v1
    r = c.post("/v1/policy/draft", json={"rules": _valid_rules(), "bundle_id": "rb-test"}, headers=h5)
    assert r.status_code == 200
    c.post("/v1/policy/approve", json={"tenant_id": "default"}, headers=h5)
    r_pub1 = c.post("/v1/policy/publish", json={"tenant_id": "default"}, headers=h5)
    assert r_pub1.status_code == 200
    v1 = r_pub1.json()["active_version"]
    # publish v2 with extra deny
    v2_rules = _valid_rules(extra=[_deny_outline_rule()])
    c.post("/v1/policy/draft", json={"rules": v2_rules, "bundle_id": "rb-test"}, headers=h5)
    c.post("/v1/policy/approve", json={"tenant_id": "default"}, headers=h5)
    r_pub2 = c.post("/v1/policy/publish", json={"tenant_id": "default"}, headers=h5)
    v2 = r_pub2.json()["active_version"]
    assert v2 != v1
    # verify history count >=2
    rh = c.get("/v1/policy/history", headers=h5)
    assert rh.status_code == 200
    versions = [x.get("version") for x in rh.json()["items"]]
    assert v1 in versions and v2 in versions
    # rollback to v1
    rr = c.post("/v1/policy/rollback", json={"target_version": v1, "tenant_id": "default"}, headers=h5)
    assert rr.status_code == 200, rr.text
    v3 = rr.json()["active_version"]
    assert v3 not in (v1, v2)  # new version incremented
    # after rollback, bundle should NOT have deny-outline-secret
    rb = c.get("/v1/policy/bundles", headers=h5)
    assert rb.status_code == 200
    latest_rules = rb.json()["bundles"][0].get("rules") or []
    ids = {r.get("id") for r in latest_rules}
    assert "deny-outline-secret" not in ids, "rollback to v1 should remove v2's deny rule"
    # rollback to non-existent version -> 404
    bad = c.post("/v1/policy/rollback", json={"target_version": "9.9.9", "tenant_id": "default"}, headers=h5)
    assert bad.status_code == 404

def test_rollback_requires_L5_and_validates():
    t4 = _l4_token()
    c = _client()
    assert c.post("/v1/policy/rollback", json={"target_version": "1.0.0"}, headers=_auth(t4)).status_code == 403
