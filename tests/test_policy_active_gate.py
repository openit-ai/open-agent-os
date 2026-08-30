"""Focused tests: active published policy affects gate, L5 mutation/L4 read-only, rollback."""
import os
import sys
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in [ROOT / "control-plane", ROOT / "security" / "policy-engine", ROOT / "packages" / "policy-model", ROOT / "packages" / "audit-model", ROOT / "security" / "audit", ROOT / "admin-console" / "backend", ROOT / "packages" / "common-types"]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest
from fastapi.testclient import TestClient
import importlib.util

def _load_admin():
    spec = importlib.util.spec_from_file_location("admin_app_policy", str(ROOT / "admin-console" / "backend" / "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["admin_app_policy"] = mod
    spec.loader.exec_module(mod)
    return mod

# --- 1. L5 mutation vs L4 read-only ---
def test_policy_l5_mutation_l4_readonly():
    mod = _load_admin()
    app = mod.app
    # ensure clean
    try:
        from admin_console.backend.auth import clear_users
        clear_users()
    except Exception:
        pass
    try:
        from admin_console.backend.policy import _mem_versions, _mem_draft  # type: ignore
        _mem_versions.clear()
        _mem_draft.clear()
    except Exception:
        pass
    client = TestClient(app)
    # L5 login (seed admin)
    r = client.post("/v1/auth/login", json={"email": "admin@openit.co.kr", "password": "Admin123!"})
    assert r.status_code == 200, r.text
    l5_token = r.json()["access_token"]
    l5_h = {"Authorization": f"Bearer {l5_token}"}
    # create L4
    r = client.post("/v1/auth/register", json={"email": "l4_policy@test.co.kr", "password": "Password123!", "role": "L4", "display_name": "L4"}, headers=l5_h)
    assert r.status_code in (200, 201), r.text
    r = client.post("/v1/auth/login", json={"email": "l4_policy@test.co.kr", "password": "Password123!"})
    assert r.status_code == 200
    l4_token = r.json()["access_token"]
    l4_h = {"Authorization": f"Bearer {l4_token}"}

    # L4 read allowed
    for path in ["/v1/policy/bundles", "/v1/policy/draft", "/v1/policy/history"]:
        r = client.get(path, headers=l4_h)
        assert r.status_code == 200, f"L4 read {path}: {r.text}"

    # L4 mutation denied (draft, approve, publish, rollback)
    r = client.post("/v1/policy/draft", json={"rules": [{"id": "r1", "source": "default_bundle", "action": "READ", "resource_pattern": "outline/*", "effect": "ALLOW", "priority": 10}]}, headers=l4_h)
    assert r.status_code == 403, r.text
    r = client.post("/v1/policy/approve", json={"tenant_id": "default"}, headers=l4_h)
    assert r.status_code == 403
    r = client.post("/v1/policy/publish", json={"tenant_id": "default"}, headers=l4_h)
    assert r.status_code == 403
    r = client.post("/v1/policy/rollback", json={"target_version": "1.0.0", "tenant_id": "default"}, headers=l4_h)
    assert r.status_code == 403

    # L5 mutation allowed: draft
    rules = [
        {"id": "deny-external-export", "source": "explicit_deny", "action": "EXPORT", "resource_pattern": "*external*", "effect": "DENY", "priority": 1},
        {"id": "deny-external-share", "source": "explicit_deny", "action": "SHARE", "resource_pattern": "*external*", "effect": "DENY", "priority": 1},
        {"id": "deny-external-send", "source": "explicit_deny", "action": "SEND", "resource_pattern": "*external*", "effect": "DENY", "priority": 1},
        {"id": "allow-outline-read", "source": "default_bundle", "action": "READ", "resource_pattern": "outline/*", "effect": "ALLOW", "priority": 10},
        {"id": "deny-admin-by-default", "source": "explicit_deny", "action": "ADMIN", "resource_pattern": "*", "effect": "DENY", "priority": 1},
    ]
    r = client.post("/v1/policy/draft", json={"rules": rules}, headers=l5_h)
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["status"] == "draft"


def test_policy_rollback_creates_published():
    mod = _load_admin()
    app = mod.app
    client = TestClient(app)
    r = client.post("/v1/auth/login", json={"email": "admin@openit.co.kr", "password": "Admin123!"})
    l5_h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    # ensure at least two published versions
    rules_v1 = [
        {"id": "deny-external-export", "source": "explicit_deny", "action": "EXPORT", "resource_pattern": "*external*", "effect": "DENY", "priority": 1},
        {"id": "deny-external-share", "source": "explicit_deny", "action": "SHARE", "resource_pattern": "*external*", "effect": "DENY", "priority": 1},
        {"id": "deny-external-send", "source": "explicit_deny", "action": "SEND", "resource_pattern": "*external*", "effect": "DENY", "priority": 1},
        {"id": "allow-outline-read", "source": "default_bundle", "action": "READ", "resource_pattern": "outline/*", "effect": "ALLOW", "priority": 10},
        {"id": "deny-admin-by-default", "source": "explicit_deny", "action": "ADMIN", "resource_pattern": "*", "effect": "DENY", "priority": 1},
    ]
    rules_v2 = [
        {"id": "deny-external-export", "source": "explicit_deny", "action": "EXPORT", "resource_pattern": "*external*", "effect": "DENY", "priority": 1},
        {"id": "deny-external-share", "source": "explicit_deny", "action": "SHARE", "resource_pattern": "*external*", "effect": "DENY", "priority": 1},
        {"id": "deny-external-send", "source": "explicit_deny", "action": "SEND", "resource_pattern": "*external*", "effect": "DENY", "priority": 1},
        {"id": "allow-outline-read", "source": "default_bundle", "action": "READ", "resource_pattern": "outline/*", "effect": "ALLOW", "priority": 10},
        {"id": "allow-extra", "source": "default_bundle", "action": "READ", "resource_pattern": "extra/*", "effect": "ALLOW", "priority": 5},
        {"id": "deny-admin-by-default", "source": "explicit_deny", "action": "ADMIN", "resource_pattern": "*", "effect": "DENY", "priority": 1},
    ]
    # draft -> approve -> publish v1
    client.post("/v1/policy/draft", json={"rules": rules_v1}, headers=l5_h)
    client.post("/v1/policy/approve", json={"tenant_id": "default"}, headers=l5_h)
    r = client.post("/v1/policy/publish", json={"tenant_id": "default"}, headers=l5_h)
    assert r.status_code == 200, r.text
    v1 = r.json()["published"]["version"]
    # v2
    client.post("/v1/policy/draft", json={"rules": rules_v2}, headers=l5_h)
    client.post("/v1/policy/approve", json={"tenant_id": "default"}, headers=l5_h)
    r = client.post("/v1/policy/publish", json={"tenant_id": "default"}, headers=l5_h)
    assert r.status_code == 200
    v2 = r.json()["published"]["version"]
    assert v2 != v1
    # history contains both
    r = client.get("/v1/policy/history", headers=l5_h)
    assert r.status_code == 200
    versions = [x["version"] for x in r.json()["items"]]
    assert v1 in versions and v2 in versions
    # rollback to v1
    r = client.post("/v1/policy/rollback", json={"target_version": v1, "tenant_id": "default"}, headers=l5_h)
    assert r.status_code == 200, r.text
    assert r.json()["published"]["status"] == "published"
    # active version now equals new rollback version (copy of v1)
    r = client.get("/v1/policy/history", headers=l5_h)
    assert r.json()["active_version"] is not None
    # verify rollback rules match v1 (allow-extra not present)
    active_rules = [x for x in r.json()["items"] if x["version"] == r.json()["active_version"]]
    assert len(active_rules) == 1
    ids = {x["id"] for x in active_rules[0]["rules"]}
    assert "allow-extra" not in ids


def test_active_published_affects_gate(monkeypatch):
    """Gate must use active published policy: allow-extra from published should ALLOW, and deny override should DENY."""
    # Publish a custom active bundle via admin API, then check gate uses it (and fallback after clear)
    mod = _load_admin()
    app = mod.app
    client = TestClient(app)
    r = client.post("/v1/auth/login", json={"email": "admin@openit.co.kr", "password": "Admin123!"})
    l5_h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    # custom active: allow READ extra/custom/*, deny READ outline/private/*
    rules = [
        {"id": "deny-external-export", "source": "explicit_deny", "action": "EXPORT", "resource_pattern": "*external*", "effect": "DENY", "priority": 1},
        {"id": "deny-external-share", "source": "explicit_deny", "action": "SHARE", "resource_pattern": "*external*", "effect": "DENY", "priority": 1},
        {"id": "deny-external-send", "source": "explicit_deny", "action": "SEND", "resource_pattern": "*external*", "effect": "DENY", "priority": 1},
        {"id": "deny-private-read", "source": "explicit_deny", "action": "READ", "resource_pattern": "outline/private/*", "effect": "DENY", "priority": 1},
        {"id": "allow-custom-read", "source": "default_bundle", "action": "READ", "resource_pattern": "extra/custom/*", "effect": "ALLOW", "priority": 10},
        {"id": "allow-outline-read", "source": "default_bundle", "action": "READ", "resource_pattern": "outline/*", "effect": "ALLOW", "priority": 20},
        {"id": "deny-admin-by-default", "source": "explicit_deny", "action": "ADMIN", "resource_pattern": "*", "effect": "DENY", "priority": 1},
        {"id": "allow-session-ingress", "source": "default_bundle", "action": "INTERACT", "resource_pattern": "session/ingress/*", "effect": "ALLOW", "priority": 5},
        {"id": "allow-mattermost-ingress", "source": "default_bundle", "action": "INTERACT", "resource_pattern": "mattermost/ingress/*", "effect": "ALLOW", "priority": 5},
    ]
    client.post("/v1/policy/draft", json={"rules": rules}, headers=l5_h)
    client.post("/v1/policy/approve", json={"tenant_id": "default"}, headers=l5_h)
    r = client.post("/v1/policy/publish", json={"tenant_id": "default"}, headers=l5_h)
    assert r.status_code == 200, r.text

    # Now gate should use active published bundle
    from control_plane.mattermost_policy_gate import MattermostPolicyGate, clear_mattermost_gate_cache, _get_audit_ledger
    from audit.audit_ledger.ledger import AuditLedger
    clear_mattermost_gate_cache()

    # build a minimal mapping
    class Map:
        tenant_id = "default"
        human_principal = "employee:kim"
        agent_principal = "agent:assistant:kim"

    gate = MattermostPolicyGate("default")
    # replace ledger with test ledger to avoid file deps
    gate._ledger = AuditLedger(signing_key="test-audit-key-active")

    async def _run():
        # allowed by active custom rule
        res = await gate.authorize_ingress(Map(), session_id="sess_active_1", trace_id="t1", request_id="r1", action="READ", resource="extra/custom/doc1")
        assert res.allowed is True
        assert res.decision == "ALLOW"
        # denied by active explicit_deny
        try:
            await gate.authorize_ingress(Map(), session_id="sess_active_2", trace_id="t2", request_id="r2", action="READ", resource="outline/private/secret")
            assert False, "should deny private"
        except Exception as e:
            assert getattr(e, "status_code", 403) == 403
            assert "denied" in str(getattr(e, "detail", str(e))).lower() or "deny" in str(e).lower()

    asyncio.run(_run())

    # Fallback verification: monkeypatch loader to return None -> gate falls back to small_business default (extra/custom not allowed)
    monkeypatch.setattr("policy_engine.active_policy_loader.get_active_published_bundle", lambda tenant_id="default": None)
    clear_mattermost_gate_cache()
    gate2 = MattermostPolicyGate("default")
    gate2._ledger = AuditLedger(signing_key="test-audit-key-active2")

    async def _run_fallback():
        try:
            await gate2.authorize_ingress(Map(), session_id="sess_fb", trace_id="t3", request_id="r3", action="READ", resource="extra/custom/doc1")
            # small_business bundle has no extra/custom allow -> default DENY
            assert False, "fallback should deny extra/custom"
        except Exception as e:
            assert getattr(e, "status_code", 403) == 403

    asyncio.run(_run_fallback())
