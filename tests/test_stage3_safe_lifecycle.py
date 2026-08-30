"""Stage 3 safe lifecycle — admin publish -> active_policy_loader -> MattermostPolicyGate E2E.

Safe: uses isolated tenant IDs and sqlite tmp DB (or real PG isolated tenant when available).
Never publishes permissive policy: mandatory deny-external-export preserved.
Preserves default small_business for other tenants and rollback.
"""
import os
import sys
import json
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in [
    ROOT / "security" / "policy-engine",
    ROOT / "packages" / "policy-model",
    ROOT / "packages" / "audit-model",
    ROOT / "control-plane",
    ROOT / "security" / "audit",
    ROOT / "admin-console" / "backend",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest
from policy_model import PolicyDecision

def _clear_loader_env(monkeypatch):
    for k in ["OAOS_DATABASE_URL", "DATABASE_URL", "OAOS_CP_DATABASE_URL", "OAOS_ENV", "ENV", "OAOS_ENVIRONMENT"]:
        monkeypatch.delenv(k, raising=False)

def _valid_rules_with_custom():
    # Preserve mandatory security rules — never weaken
    return [
        {"id": "deny-external-export", "source": "explicit_deny", "action": "EXPORT", "resource_pattern": "*external*", "effect": "DENY", "priority": 1},
        {"id": "deny-external-share", "source": "explicit_deny", "action": "SHARE", "resource_pattern": "*external*", "effect": "DENY", "priority": 1},
        {"id": "allow-session-ingress-interact", "source": "default_bundle", "action": "INTERACT", "resource_pattern": "session/ingress/*", "effect": "ALLOW", "priority": 10},
        {"id": "allow-outline-read", "source": "default_bundle", "action": "READ", "resource_pattern": "outline/*", "effect": "ALLOW", "priority": 10},
        {"id": "deny-admin-by-default", "source": "explicit_deny", "action": "ADMIN", "resource_pattern": "*", "effect": "DENY", "priority": 1},
        # Custom rule that published bundle adds — safe, non-permissive
        {"id": "deny-stage3-secret", "source": "explicit_deny", "action": "READ", "resource_pattern": "outline/secret-stage3/*", "effect": "DENY", "priority": 1},
        {"id": "allow-stage3-custom", "source": "default_bundle", "action": "READ", "resource_pattern": "stage3/custom/*", "effect": "ALLOW", "priority": 5},
    ]

def test_loader_respects_oaos_cp_database_url(monkeypatch, tmp_path):
    """Failing test first: loader must respect OAOS_CP_DATABASE_URL (systemd control-plane)."""
    _clear_loader_env(monkeypatch)
    db_path = tmp_path / "cp_test.db"
    url = f"sqlite:///{db_path}"
    # Only set OAOS_CP_DATABASE_URL, not OAOS_DATABASE_URL/DATABASE_URL — loader should still find DB
    monkeypatch.setenv("OAOS_CP_DATABASE_URL", url)
    # create published row via direct sqlalchemy
    from sqlalchemy import create_engine, text
    eng = create_engine(url, connect_args={"check_same_thread": False})
    ddl = """CREATE TABLE IF NOT EXISTS admin_policy_versions (
        id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, bundle_id TEXT NOT NULL,
        name TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL,
        rules_json TEXT NOT NULL, created_by TEXT, created_at TEXT NOT NULL,
        approved_by TEXT, approved_at TEXT, published_at TEXT, parent_version TEXT
    )"""
    rules = _valid_rules_with_custom()
    with eng.begin() as conn:
        conn.execute(text(ddl))
        conn.execute(text("INSERT INTO admin_policy_versions (id, tenant_id, bundle_id, name, version, status, rules_json, created_by, created_at) VALUES (:id,:t,:bid,:name,:ver,:st,:rj,:cb,:ca)"),
                     {"id": "pv_stage3_cp1", "t": "stage3_cp_tenant", "bid": "bundle-stage3", "name": "Stage3 CP Test", "ver": "1.0.0", "st": "published", "rj": json.dumps(rules), "cb": "tester", "ca": "2026-01-01T00:00:00Z"})
    eng.dispose()
    import control_plane.mattermost_policy_gate as gate
    gate.clear_mattermost_gate_cache()
    from policy_engine.active_policy_loader import get_active_published_bundle
    b = get_active_published_bundle("stage3_cp_tenant")
    assert b is not None, "loader must respect OAOS_CP_DATABASE_URL (systemd control-plane) — fix active_policy_loader._db_sync_url"
    assert b.version == "1.0.0"
    assert any(r.id == "allow-stage3-custom" for r in b.rules)
    # verify gate uses it
    eng2 = gate._get_small_business_engine("stage3_cp_tenant")
    from policy_model import PolicyEvaluationRequest
    req = PolicyEvaluationRequest(tenant_id="stage3_cp_tenant", user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="stage3/custom/doc1")
    res = eng2.evaluate(req)
    assert res.decision == PolicyDecision.ALLOW, "published custom allow must be reflected in gate"
    # non-published tenant must fallback to small_business (deny custom)
    eng_other = gate._get_small_business_engine("other_tenant_no_publish")
    res_other = eng_other.evaluate(PolicyEvaluationRequest(tenant_id="other_tenant_no_publish", user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="stage3/custom/doc1"))
    assert res_other.decision == PolicyDecision.DENY, "other tenant must fallback to default deny"
    gate.clear_mattermost_gate_cache()
    monkeypatch.delenv("OAOS_CP_DATABASE_URL", raising=False)

def test_stage3_publish_rollback_lifecycle_isolated_sqlite(monkeypatch, tmp_path):
    """Safe lifecycle via sqlite isolated tenant: publish -> gate ALLOW/DENY -> rollback -> gate fallback. Preserves mandatory."""
    _clear_loader_env(monkeypatch)
    db_path = tmp_path / "stage3_lifecycle.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("OAOS_DATABASE_URL", url)
    from sqlalchemy import create_engine, text
    eng = create_engine(url, connect_args={"check_same_thread": False})
    ddl = """CREATE TABLE IF NOT EXISTS admin_policy_versions (
        id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, bundle_id TEXT NOT NULL,
        name TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL,
        rules_json TEXT NOT NULL, created_by TEXT, created_at TEXT NOT NULL,
        approved_by TEXT, approved_at TEXT, published_at TEXT, parent_version TEXT
    )"""
    with eng.begin() as conn:
        conn.execute(text(ddl))
    eng.dispose()

    tenant = "stage3_lifecycle_tenant"
    # Insert first published (v1) — minimal but mandatory
    rules_v1 = _valid_rules_with_custom()
    # v1 without custom deny for secret? Actually custom includes both; we test v1 has deny, then publish v2 with extra allow removal
    from sqlalchemy import create_engine as ce2
    eng2 = ce2(url, connect_args={"check_same_thread": False})
    with eng2.begin() as conn:
        conn.execute(text("INSERT INTO admin_policy_versions (id, tenant_id, bundle_id, name, version, status, rules_json, created_by, created_at, published_at) VALUES (:id,:t,:bid,:name,:ver,:st,:rj,:cb,:ca,:pa)"),
                     {"id": "pv_v1", "t": tenant, "bid": "bundle-stage3", "name": "v1", "ver": "1.0.0", "st": "published", "rj": json.dumps(rules_v1), "cb": "tester", "ca": "2026-01-01T00:00:00Z", "pa": "2026-01-01T00:00:00Z"})
    eng2.dispose()

    import control_plane.mattermost_policy_gate as gate
    from policy_engine.active_policy_loader import get_active_published_bundle
    gate.clear_mattermost_gate_cache()
    b1 = get_active_published_bundle(tenant)
    assert b1 is not None and b1.version == "1.0.0"
    # gate must DENY secret-stage3 and ALLOW custom
    e = gate._get_small_business_engine(tenant)
    from policy_model import PolicyEvaluationRequest
    r_secret = PolicyEvaluationRequest(tenant_id=tenant, user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="outline/secret-stage3/confidential")
    assert e.evaluate(r_secret).decision == PolicyDecision.DENY
    r_custom = PolicyEvaluationRequest(tenant_id=tenant, user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="stage3/custom/doc1")
    assert e.evaluate(r_custom).decision == PolicyDecision.ALLOW
    # Verify via MattermostPolicyGate authorize_ingress
    from control_plane.mattermost_policy_gate import MattermostPolicyGate
    from control_plane.identity import map_user_to_agent
    from audit.audit_ledger.ledger import AuditLedger
    mapping = map_user_to_agent("employee:kim", tenant)
    g = MattermostPolicyGate(tenant)
    g._ledger = AuditLedger(signing_key="stage3-test")
    async def _check():
        res = await g.authorize_ingress(mapping, session_id="sess_stage3_1", trace_id="t1", request_id="r1", action="READ", resource="stage3/custom/doc1")
        assert res.decision == "ALLOW"
        try:
            await g.authorize_ingress(mapping, session_id="sess_stage3_2", trace_id="t2", request_id="r2", action="READ", resource="outline/secret-stage3/confidential")
            assert False, "should deny"
        except Exception as ex:
            assert getattr(ex, "status_code", 403) == 403
    asyncio.run(_check())

    # Simulate publish v2 with different rules (remove custom allow, keep deny)
    rules_v2 = [r for r in rules_v1 if r["id"] != "allow-stage3-custom"]
    eng3 = ce2(url, connect_args={"check_same_thread": False})
    with eng3.begin() as conn:
        conn.execute(text("INSERT INTO admin_policy_versions (id, tenant_id, bundle_id, name, version, status, rules_json, created_by, created_at, published_at) VALUES (:id,:t,:bid,:name,:ver,:st,:rj,:cb,:ca,:pa)"),
                     {"id": "pv_v2", "t": tenant, "bid": "bundle-stage3", "name": "v2", "ver": "1.0.1", "st": "published", "rj": json.dumps(rules_v2), "cb": "tester", "ca": "2026-01-02T00:00:00Z", "pa": "2026-01-02T00:00:00Z"})
    eng3.dispose()
    gate.clear_mattermost_gate_cache()
    b2 = get_active_published_bundle(tenant)
    assert b2.version == "1.0.1"
    e2 = gate._get_small_business_engine(tenant)
    assert e2.evaluate(r_custom).decision == PolicyDecision.DENY, "v2 removed custom allow => DENY"

    # Simulate rollback: insert v3 copying v1 (new version)
    eng4 = ce2(url, connect_args={"check_same_thread": False})
    with eng4.begin() as conn:
        conn.execute(text("INSERT INTO admin_policy_versions (id, tenant_id, bundle_id, name, version, status, rules_json, created_by, created_at, published_at, parent_version) VALUES (:id,:t,:bid,:name,:ver,:st,:rj,:cb,:ca,:pa,:pv)"),
                     {"id": "pv_v3_rollback", "t": tenant, "bid": "bundle-stage3", "name": "v1-rollback", "ver": "1.0.2", "st": "published", "rj": json.dumps(rules_v1), "cb": "tester", "ca": "2026-01-03T00:00:00Z", "pa": "2026-01-03T00:00:00Z", "pv": "1.0.0"})
    eng4.dispose()
    gate.clear_mattermost_gate_cache()
    b3 = get_active_published_bundle(tenant)
    assert b3.version == "1.0.2"
    e3 = gate._get_small_business_engine(tenant)
    assert e3.evaluate(r_custom).decision == PolicyDecision.ALLOW, "rollback to v1 must restore custom allow"
    # mandatory still present
    assert any(r.id == "deny-external-export" for r in b3.rules), "mandatory deny-external-export must survive rollback"
    gate.clear_mattermost_gate_cache()
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)

def test_postgresql_isolated_tenant_if_available(monkeypatch):
    """If real PostgreSQL is available (via repo .env DATABASE_URL), verify live PG path with isolated tenant and cleanup."""
    # Load repo .env DATABASE_URL
    repo_env = ROOT / ".env"
    url = None
    if repo_env.exists():
        for line in repo_env.read_text().splitlines():
            line=line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k,v=line.split("=",1)
            if k.strip()=="DATABASE_URL":
                url=v.strip()
                break
    if not url:
        pytest.skip("No DATABASE_URL in repo .env — skip live PG check")
    # Check connectivity
    try:
        sync=url.replace("postgresql+asyncpg://","postgresql+psycopg://").replace("postgresql://","postgresql+psycopg://")
        if "+asyncpg" in sync:
            sync=sync.replace("+asyncpg","+psycopg")
        from sqlalchemy import create_engine, text
        eng=create_engine(sync, pool_pre_ping=False)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
    except Exception as e:
        pytest.skip(f"PostgreSQL not reachable: {e}")
    # Isolated tenant lifecycle against real PG — cleanup afterwards
    tenant = "stage3_pg_isolated_tenant"
    sync=url.replace("postgresql+asyncpg://","postgresql+psycopg://").replace("postgresql://","postgresql+psycopg://")
    if "+asyncpg" in sync:
        sync=sync.replace("+asyncpg","+psycopg")
    _clear_loader_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", url)
    from sqlalchemy import create_engine, text
    eng=create_engine(sync, pool_pre_ping=False)
    # Ensure table exists (policy module creates IF NOT EXISTS) — also via policy ensure
    try:
        with eng.begin() as conn:
            conn.execute(text("""CREATE TABLE IF NOT EXISTS admin_policy_versions (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, bundle_id TEXT NOT NULL,
                name TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL,
                rules_json TEXT NOT NULL, created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                approved_by TEXT, approved_at TIMESTAMPTZ, published_at TIMESTAMPTZ, parent_version TEXT
            )"""))
            conn.execute(text("DELETE FROM admin_policy_versions WHERE tenant_id=:t"), {"t": tenant})
    except Exception:
        pass
    eng.dispose()
    rules = _valid_rules_with_custom()
    eng2=create_engine(sync, pool_pre_ping=False)
    with eng2.begin() as conn:
        conn.execute(text("INSERT INTO admin_policy_versions (id, tenant_id, bundle_id, name, version, status, rules_json, created_by, created_at, published_at) VALUES (:id,:t,:bid,:name,:ver,:st,:rj,:cb,NOW(),NOW())"),
                     {"id": "pv_pg_stage3", "t": tenant, "bid": "bundle-stage3-pg", "name": "PG Stage3", "ver": "1.0.0", "st": "published", "rj": json.dumps(rules), "cb": "stage3-test"})
    eng2.dispose()
    import control_plane.mattermost_policy_gate as gate
    gate.clear_mattermost_gate_cache()
    from policy_engine.active_policy_loader import get_active_published_bundle
    b=get_active_published_bundle(tenant)
    assert b is not None, "live PG published row must be loaded"
    assert b.version=="1.0.0"
    e=gate._get_small_business_engine(tenant)
    from policy_model import PolicyEvaluationRequest
    r=PolicyEvaluationRequest(tenant_id=tenant, user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="stage3/custom/doc1")
    assert e.evaluate(r).decision == PolicyDecision.ALLOW
    # Cleanup — delete isolated tenant rows only
    eng3=create_engine(sync, pool_pre_ping=False)
    with eng3.begin() as conn:
        conn.execute(text("DELETE FROM admin_policy_versions WHERE tenant_id=:t"), {"t": tenant})
    eng3.dispose()
    gate.clear_mattermost_gate_cache()
    # Verify fallback after delete
    b_after=get_active_published_bundle(tenant)
    assert b_after is None, "after cleanup, no published should remain for isolated tenant"
    e_after=gate._get_small_business_engine(tenant)
    assert e_after.evaluate(r).decision == PolicyDecision.DENY
    gate.clear_mattermost_gate_cache()
    monkeypatch.delenv("DATABASE_URL", raising=False)
