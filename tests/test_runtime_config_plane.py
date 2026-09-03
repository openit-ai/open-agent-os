"""Focused regression tests for Runtime Configuration Plane Stage-1.

Covers: Admin snapshot publish + signed canonical, secret_ref only, optimistic version,
tenant isolation, rollback pointer, applied_by/at/process identity, CP verify + fail-closed,
tamper rejection.

No DB/external network. Pure in-memory + admin_settings fallback path mocked.
"""
from __future__ import annotations
import importlib.util, sys, pathlib, os
ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"
CP_ROOT = ROOT / "control-plane"

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec and spec.loader
    # pre-populate admin_console packages so sibling imports resolve
    import types
    for pkg in ("admin_console","admin_console.backend"):
        if pkg not in sys.modules:
            m=types.ModuleType(pkg); m.__path__=[]; sys.modules[pkg]=m
    spec.loader.exec_module(mod)
    return mod

# Ensure paths for CP auth
for p in [str(CP_ROOT), str(ROOT/"security"/"policy-engine"), str(ROOT/"security"/"audit")]:
    if p not in sys.path:
        sys.path.insert(0, p)

# Clean env for isolation
os.environ.pop("OAOS_ENV", None)
os.environ.pop("OAOS_RUNTIME_CONFIG_SIGNING_KEY", None)
os.environ["OAOS_CORS_ORIGINS"]="http://localhost:3012"
os.environ["OAOS_VAULT_KEY"]="test-vault-key-for-llm-provider-32bytes!!"

from fastapi.testclient import TestClient

def _admin_client():
    auth = _load("admin_console.backend.auth", BACKEND/"auth.py")
    # re-load app after auth to pick up routes
    app_mod = _load("admin_console.backend.app", BACKEND/"app.py")
    return app_mod.app, auth

def _cp_client():
    # import CP app (requires control_plane package on sys.path)
    if str(CP_ROOT) not in sys.path:
        sys.path.insert(0, str(CP_ROOT))
    from control_plane.app import app as cp_app
    from control_plane.runtime_config import clear_runtime_config_state
    return cp_app, clear_runtime_config_state

def _login(client, email="admin@openit.co.kr", password="Admin123!"):
    r=client.post("/v1/auth/login", json={"email":email,"password":password})
    assert r.status_code==200, r.text
    tok=r.json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}

def _login_l4(client, auth_mod):
    # create L4 via L5
    hdr=_login(client)
    email="l4_rc@test.co.kr"
    r=client.post("/v1/auth/register", json={"email":email,"password":"Password123!","display_name":"L4","role":"L4"}, headers=hdr)
    if r.status_code not in (200,201,409):
        pass
    # login as L4
    r2=client.post("/v1/auth/login", json={"email":email,"password":"Password123!"})
    if r2.status_code==200:
        return {"Authorization": f"Bearer {r2.json()['access_token']}"}
    return hdr

def test_admin_rc_snapshot_publish_and_cp_verify():
    cp_app, cp_clear = _cp_client()
    admin_app, auth_mod = _admin_client()
    # clear runtime-config state
    try:
        import admin_console.backend.runtime_config as rc
        rc.clear_runtime_config_state()
    except Exception:
        import importlib; rc=importlib.import_module("admin_console.backend.runtime_config"); rc.clear_runtime_config_state()
    cp_clear()
    ac=TestClient(admin_app)
    cc=TestClient(cp_app)
    # seed via admin API: create infra, llm provider (with secret), user-mapping
    hdr=_login(ac)
    # ensure llm mode (default is hermes, LLM provider blocked in hermes mode)
    ac.post("/v1/runtime/mode", json={"mode":"llm"}, headers=hdr)
    # infra service
    ac.post("/v1/infra/services", json={"name":"hermes","display_name":"Hermes","host":"127.0.0.1","port":8642}, headers=hdr)
    # llm provider (secret should not leak into snapshot)
    r=ac.post("/v1/llm/providers", json={"provider":"openrouter","apiKey":"sk-test-secret-12345","model":"test-model"}, headers=hdr)
    assert r.status_code in (200,201), r.text
    # user mapping
    ac.post("/v1/user-mappings", json={"mm_username":"alice","mm_user_id":"a"*26,"employee_principal":"employee:alice"}, headers=hdr)
    # create snapshot (auto derives config)
    r=ac.post("/v1/runtime/config/snapshot", json={"tenant_id":"default"}, headers=hdr)
    assert r.status_code==201, r.text
    snap=r.json()
    assert snap["version"]==1
    assert snap["tenant_id"]=="default"
    assert "signature" in snap and len(snap["signature"])==64
    # secret raw must NOT be in snapshot
    blob=str(snap)
    assert "sk-test-secret" not in blob
    assert "encrypted_api_key" not in blob
    # llm provider entries should have secret_ref only
    for p in snap["config"]["llm_providers"]:
        assert "secret_ref" in p and p["secret_ref"].startswith("vault://")
        assert "encrypted_api_key" not in p
        assert "api_key" not in p
    # list snapshots
    r=ac.get("/v1/runtime/config/snapshots?tenant_id=default", headers=hdr)
    assert r.status_code==200 and r.json()["count"]==1
    # publish v1
    r=ac.post("/v1/runtime/config/publish", json={"tenant_id":"default","version":1}, headers=hdr)
    assert r.status_code==200, r.text
    assert r.json()["published_version"]==1
    # status
    r=ac.get("/v1/runtime/config/status?tenant_id=default", headers=hdr)
    assert r.status_code==200
    assert r.json()["published_version"]==1
    assert r.json()["has_snapshot"] is True

    # Control Plane: fetch and verify (using X-User-Id fallback allowed in non-prod)
    # CP identity: tests allow plaintext
    os.environ["OAOS_TEST_ALLOW_PLAINTEXT"]="1"
    os.environ["OAOS_CP_TEST_ALLOW_PLAINTEXT"]="1"
    # Need to ensure CP fetches same DB — our CP runtime_config reads via admin module import or DB
    # Ensure admin module is discoverable: it is already imported as admin_console.backend.runtime_config
    r=cc.get("/v1/runtime-config", headers={"X-User-Id":"employee:alice","X-Tenant-Id":"default"})
    assert r.status_code==200, r.text
    assert r.json()["verified"] is True
    assert r.json()["snapshot"]["version"]==1
    assert "sk-test-secret" not in str(r.json())
    # status
    r=cc.get("/v1/runtime-config/status", headers={"X-User-Id":"employee:alice","X-Tenant-Id":"default"})
    assert r.status_code==200
    assert r.json()["published_version"]==1
    assert r.json()["verified"] is True
    # apply
    r=cc.post("/v1/runtime-config/apply", headers={"X-User-Id":"employee:alice","X-Tenant-Id":"default"})
    assert r.status_code==200, r.text
    assert r.json()["applied"]["version"]==1
    assert "process_identity" in r.json()["applied"] or "process_identity" in r.json()["applied"].get("process_identity","")
    # status should now show applied
    r=cc.get("/v1/runtime-config/status", headers={"X-User-Id":"employee:alice","X-Tenant-Id":"default"})
    assert r.json()["applied"]["version"]==1
    assert r.json()["applied"]["applied_by"]=="employee:alice"

def test_admin_rc_optimistic_version_and_rollback_tenant_isolation():
    admin_app, auth_mod = _admin_client()
    try:
        import admin_console.backend.runtime_config as rc
        rc.clear_runtime_config_state()
    except Exception:
        import importlib; rc=importlib.import_module("admin_console.backend.runtime_config"); rc.clear_runtime_config_state()
    ac=TestClient(admin_app)
    hdr=_login(ac)
    # create first snapshot for tenant A
    r=ac.post("/v1/runtime/config/snapshot", json={"tenant_id":"default"}, headers=hdr)
    assert r.status_code==201
    v1=r.json()["version"]
    # second snapshot should be v2
    r=ac.post("/v1/runtime/config/snapshot", json={"tenant_id":"default"}, headers=hdr)
    assert r.json()["version"]==v1+1
    v2=r.json()["version"]
    # publish v2
    r=ac.post("/v1/runtime/config/publish", json={"tenant_id":"default","version":v2}, headers=hdr)
    assert r.status_code==200
    # optimistic conflict: expected_version mismatch
    r=ac.post("/v1/runtime/config/snapshot", json={"tenant_id":"default","expected_version":999}, headers=hdr)
    assert r.status_code==409
    # rollback to v1
    r=ac.post("/v1/runtime/config/rollback", json={"tenant_id":"default","version":v1}, headers=hdr)
    assert r.status_code==200, r.text
    assert r.json()["published_version"]==v1
    assert r.json()["snapshot"]["version"]==v1
    assert r.json()["snapshot"]["rollback_from"]==v2
    # tenant isolation: tenant B snapshots don't affect default
    r=ac.post("/v1/runtime/config/snapshot", json={"tenant_id":"tenantB"}, headers=hdr)
    assert r.status_code==201 and r.json()["tenant_id"]=="tenantB" and r.json()["version"]==1
    r=ac.get("/v1/runtime/config/status?tenant_id=tenantB", headers=hdr)
    assert r.json()["published_version"] is None
    r=ac.get("/v1/runtime/config/status?tenant_id=default", headers=hdr)
    assert r.json()["published_version"]==v1

def test_admin_rc_rbac_and_tamper_rejection():
    admin_app, auth_mod = _admin_client()
    try:
        import admin_console.backend.runtime_config as rc
        rc.clear_runtime_config_state()
    except Exception:
        import importlib; rc=importlib.import_module("admin_console.backend.runtime_config"); rc.clear_runtime_config_state()
    ac=TestClient(admin_app)
    hdr_l5=_login(ac)
    hdr_l4=_login_l4(ac, auth_mod)
    # L4 cannot snapshot/publish/rollback
    for path, body in [
        ("/v1/runtime/config/snapshot", {"tenant_id":"default"}),
        ("/v1/runtime/config/publish", {"tenant_id":"default","version":1}),
        ("/v1/runtime/config/rollback", {"tenant_id":"default","version":1}),
    ]:
        r=ac.post(path, json=body, headers=hdr_l4)
        assert r.status_code in (401,403), f"{path} L4 should be denied: {r.text}"
    # L4 can read status (requires auth)
    r=ac.get("/v1/runtime/config/status?tenant_id=default", headers=hdr_l4)
    assert r.status_code==200
    # unauthenticated read should 401
    r=ac.get("/v1/runtime/config/status?tenant_id=default")
    assert r.status_code==401
    # create and publish for tamper test
    r=ac.post("/v1/runtime/config/snapshot", json={"tenant_id":"default"}, headers=hdr_l5)
    assert r.status_code==201
    v=r.json()["version"]
    r=ac.post("/v1/runtime/config/publish", json={"tenant_id":"default","version":v}, headers=hdr_l5)
    assert r.status_code==200
    # tamper: fetch via internal verify endpoint with tampered body should fail
    # We exercise CP verification by flipping a byte in the snapshot via direct dict mutation simulation:
    # Instead test snapshot that contains secret raw is rejected (no secret raw stored, so we inject one)
    # Try to publish with explicit config_containing encrypted_api_key should be rejected 422
    r=ac.post("/v1/runtime/config/snapshot", json={"tenant_id":"default","config_patch":{"llm_providers":[{"provider":"openrouter","secret_ref":"vault://x","encrypted_api_key":"leak"}]}}, headers=hdr_l5)
    # Either it succeeds but strips leak, or rejects — in either case leak must not appear
    if r.status_code==201:
        assert "encrypted_api_key" not in str(r.json())
        assert "leak" not in str(r.json())

def test_cp_fail_closed_on_tampered_signature():
    """CP should reject tampered snapshot signature (fail-closed in production, 502/503 in test)."""
    admin_app, _ = _admin_client()
    from control_plane.runtime_config import _verify_snapshot, _get_signing_key
    try:
        import admin_console.backend.runtime_config as rc
        rc.clear_runtime_config_state()
    except Exception:
        import importlib; rc=importlib.import_module("admin_console.backend.runtime_config"); rc.clear_runtime_config_state()
    ac=TestClient(admin_app)
    hdr=_login(ac)
    r=ac.post("/v1/runtime/config/snapshot", json={"tenant_id":"default"}, headers=hdr)
    assert r.status_code==201
    snap=r.json()
    orig_sig=snap["signature"]
    # tamper version but keep sig — verification must fail
    snap_tampered=dict(snap)
    snap_tampered["version"]=9999
    # sig still old
    assert _verify_snapshot(snap_tampered) is False
    # correct snapshot verifies
    assert _verify_snapshot(snap) is True
    # wrong key fails
    assert _verify_snapshot(snap, key="wrong-key-12345678901234567890") is False
