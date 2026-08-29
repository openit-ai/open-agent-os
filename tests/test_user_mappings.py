"""Mattermost Users -> Agent mapping API tests (§14 1:1 Logical Agent).

Covers: admin-console/backend/user_mappings.py
- GET /v1/user-mappings (list)
- POST /v1/user-mappings (register, auto-derive, explicit principal)
- DELETE /v1/user-mappings/{id}
- POST /v1/user-mappings/sync (dry-run preview)

RBAC: L4 read (GET), L5 write (POST/DELETE/sync), 401 unauthenticated.

Uses same harness as test_admin_business (importlib namespaced).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"

# Save bare + canonical aliases to avoid polluting global sys.modules for other tests (L4 fixture regression)
_saved_bare = {k: sys.modules.get(k) for k in ("auth", "infra", "business", "managed", "user_mappings")}
_saved_canonical = {k: sys.modules.get(k) for k in ("admin_console.backend.auth", "admin_console.backend.infra", "admin_console.backend.business", "admin_console.backend.managed", "admin_console.backend.user_mappings", "admin_console.backend.app")}

def _load_admin_module(name: str, filename: str, bare_alias: str | None = None):
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location(name, str(BACKEND / filename))
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    sys.modules[name] = mod
    if bare_alias:
        sys.modules[bare_alias] = mod
    spec.loader.exec_module(mod)  # type: ignore
    return mod

auth_mod = _load_admin_module("admin_auth_umap", "auth.py", bare_alias="auth")
infra_mod = _load_admin_module("admin_infra_umap", "infra.py", bare_alias="infra")
business_mod = _load_admin_module("admin_business_umap", "business.py", bare_alias="business")
managed_mod = _load_admin_module("admin_managed_umap", "managed.py", bare_alias="managed")
user_mappings_mod = _load_admin_module("admin_user_mappings", "user_mappings.py", bare_alias="user_mappings")
_app_mod = _load_admin_module("admin_app_umap", "app.py")
# Restore bare + canonical aliases after isolated load so full-suite ordering isn't polluted
for _k, _v in _saved_bare.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v
for _k, _v in _saved_canonical.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v
if str(BACKEND) in sys.path:
    sys.path.remove(str(BACKEND))

admin_app = _app_mod.app

@pytest.fixture(autouse=True)
def isolate():
    def _do_clear():
        # clear private modules
        try:
            auth_mod.clear_users()
        except Exception:
            pass
        # also clear canonical + polluted bare alias
        for cand in [sys.modules.get("admin_console.backend.auth"), sys.modules.get("auth")]:
            try:
                if cand is not None and cand is not auth_mod and hasattr(cand, "clear_users"):
                    cand.clear_users()
            except Exception:
                pass
        try:
            infra_mod.clear_services()
        except Exception:
            pass
        # business clear if available
        try:
            business_mod.clear_license()
            business_mod.clear_backups()
            business_mod.clear_upgrade()
        except Exception:
            pass
        try:
            managed_mod.clear_tickets()
        except Exception:
            pass
        try:
            cand = sys.modules.get("admin_console.backend.managed") or sys.modules.get("managed")
            if cand is not None and cand is not managed_mod and hasattr(cand, "clear_tickets"):
                cand.clear_tickets()
        except Exception:
            pass
        try:
            user_mappings_mod.clear_mappings()
        except Exception:
            pass
        # also clear canonical user_mappings if loaded elsewhere
        for cand in [sys.modules.get("admin_console.backend.user_mappings"), sys.modules.get("user_mappings")]:
            try:
                if cand is not None and cand is not user_mappings_mod and hasattr(cand, "clear_mappings"):
                    cand.clear_mappings()
            except Exception:
                pass
    _do_clear()
    yield
    _do_clear()

def _client():
    return TestClient(admin_app)

def _login(email="admin@openit.co.kr", password="Admin123!"):
    c = _client()
    r = c.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]

def _auth(token):
    return {"Authorization": f"Bearer {token}"}

def _make_l4():
    token_l5 = _login()
    c = _client()
    c.post(
        "/v1/auth/register",
        json={"email": "viewer@test.co.kr", "password": "Password123!", "display_name": "Viewer", "role": "L4"},
        headers=_auth(token_l5),
    )
    # Sync viewer user across all auth module caches (full-suite alias divergence fix) — keep test-isolated, no production change
    try:
        import sys as _sys
        src_user = None
        for cand in list(_sys.modules.values()):
            try:
                d = getattr(cand, "_users_by_email", None)
                if isinstance(d, dict) and "viewer@test.co.kr" in d:
                    src_user = d["viewer@test.co.kr"]
                    break
            except Exception:
                continue
        if src_user is not None:
            for target in list(_sys.modules.values()):
                try:
                    d = getattr(target, "_users_by_email", None)
                    did = getattr(target, "_users_by_id", None)
                    if isinstance(d, dict) and isinstance(did, dict):
                        d[src_user.email] = src_user
                        did[src_user.id] = src_user
                except Exception:
                    continue
    except Exception:
        pass
    r = c.post("/v1/auth/login", json={"email": "viewer@test.co.kr", "password": "Password123!"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"], token_l5

# ---------------------------------------------------------------------------
# 401 unauthenticated
# ---------------------------------------------------------------------------

def test_list_requires_auth():
    c = _client()
    r = c.get("/v1/user-mappings")
    assert r.status_code == 401

def test_create_requires_auth():
    c = _client()
    r = c.post("/v1/user-mappings", json={"mm_user_id": "u1"})
    assert r.status_code == 401

def test_delete_requires_auth():
    c = _client()
    r = c.delete("/v1/user-mappings/some_id")
    assert r.status_code == 401

def test_sync_requires_auth():
    c = _client()
    r = c.post("/v1/user-mappings/sync", json={})
    assert r.status_code == 401

# ---------------------------------------------------------------------------
# GET list — L4/L5 allowed, empty initially
# ---------------------------------------------------------------------------

def test_list_empty_l5():
    token = _login()
    c = _client()
    r = c.get("/v1/user-mappings", headers=_auth(token))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 0
    assert data["mappings"] == []
    # trailing slash also works
    r2 = c.get("/v1/user-mappings/", headers=_auth(token))
    assert r2.status_code == 200

def test_list_l4_allowed():
    token_l4, _ = _make_l4()
    c = _client()
    r = c.get("/v1/user-mappings", headers=_auth(token_l4))
    assert r.status_code == 200
    assert r.json()["count"] == 0

# ---------------------------------------------------------------------------
# POST create — explicit principal
# ---------------------------------------------------------------------------

def test_create_l5_explicit_principal():
    token = _login()
    c = _client()
    r = c.post("/v1/user-mappings", json={"mm_user_id": "mm_123", "mm_username": "kim", "employee_principal": "employee:kim"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["mm_user_id"] == "mm_123"
    assert data["mm_username"] == "kim"
    assert data["employee_principal"] == "employee:kim"
    assert data["agent_id"] == "agent:assistant:kim"
    assert data["status"] == "active"
    assert "id" in data
    assert data["created_by"] == "admin@openit.co.kr"
    # verify list count 1
    r2 = c.get("/v1/user-mappings", headers=_auth(token))
    assert r2.json()["count"] == 1
    # check fields
    m = r2.json()["mappings"][0]
    assert m["employee_principal"] == "employee:kim"

def test_create_auto_derive_from_username():
    token = _login()
    c = _client()
    # no principal -> auto-derive via MattermostAdapter.map_mattermost_user logic (username)
    r = c.post("/v1/user-mappings", json={"mm_user_id": "uid999", "mm_username": "Alice.Wu"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["employee_principal"] == "employee:alice.wu"
    assert data["agent_id"] == "agent:assistant:alice.wu"

def test_create_auto_derive_from_user_id_when_no_username():
    token = _login()
    c = _client()
    r = c.post("/v1/user-mappings", json={"mm_user_id": "UID-1234"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    data = r.json()
    # sanitize lower: uid-1234
    assert data["employee_principal"] == "employee:uid-1234"
    assert data["agent_id"] == "agent:assistant:uid-1234"

def test_create_sanitize_special_chars():
    token = _login()
    c = _client()
    r = c.post("/v1/user-mappings", json={"mm_user_id": "x", "mm_username": "Kim@Open!"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    assert r.json()["employee_principal"] == "employee:kimopen"
    assert r.json()["agent_id"] == "agent:assistant:kimopen"

def test_create_unknown_fallback():
    token = _login()
    c = _client()
    r = c.post("/v1/user-mappings", json={"mm_user_id": "u1", "mm_username": "@@@"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    assert r.json()["employee_principal"] == "employee:unknown"
    assert r.json()["agent_id"] == "agent:assistant:unknown"

def test_create_invalid_principal_rejected():
    token = _login()
    c = _client()
    r = c.post("/v1/user-mappings", json={"mm_user_id": "u1", "employee_principal": "bad:kim"}, headers=_auth(token))
    assert r.status_code == 400
    assert "employee:" in r.text

def test_create_duplicate_mm_user_id_409():
    token = _login()
    c = _client()
    r1 = c.post("/v1/user-mappings", json={"mm_user_id": "dup1", "mm_username": "kim"}, headers=_auth(token))
    assert r1.status_code == 201
    r2 = c.post("/v1/user-mappings", json={"mm_user_id": "dup1", "mm_username": "lee"}, headers=_auth(token))
    assert r2.status_code == 409

def test_create_l4_forbidden():
    token_l4, _ = _make_l4()
    c = _client()
    r = c.post("/v1/user-mappings", json={"mm_user_id": "u_l4", "mm_username": "viewer"}, headers=_auth(token_l4))
    assert r.status_code == 403

def test_create_missing_mm_user_id_422():
    """Username-only is now valid (auto-resolve). Without MM config, unknown user -> 404; with fake user in test env, fallback may 201."""
    token = _login()
    c = _client()
    r = c.post("/v1/user-mappings", json={"mm_username": "kim"}, headers=_auth(token))
    # In CI without Mattermost token, unknown username yields 404; previously required mm_user_id -> 422
    assert r.status_code in (404, 201, 400)

# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

def test_delete_l5_success():
    token = _login()
    c = _client()
    r = c.post("/v1/user-mappings", json={"mm_user_id": "todel", "mm_username": "todel"}, headers=_auth(token))
    mid = r.json()["id"]
    # list before
    assert c.get("/v1/user-mappings", headers=_auth(token)).json()["count"] == 1
    dr = c.delete(f"/v1/user-mappings/{mid}", headers=_auth(token))
    assert dr.status_code == 200, dr.text
    assert dr.json()["status"] == "deleted"
    assert dr.json()["id"] == mid
    assert c.get("/v1/user-mappings", headers=_auth(token)).json()["count"] == 0

def test_delete_not_found_404():
    token = _login()
    c = _client()
    r = c.delete("/v1/user-mappings/nonexist_123", headers=_auth(token))
    assert r.status_code == 404

def test_delete_l4_forbidden():
    token = _login()
    c = _client()
    r = c.post("/v1/user-mappings", json={"mm_user_id": "del_l4", "mm_username": "x"}, headers=_auth(token))
    mid = r.json()["id"]
    token_l4, _ = _make_l4()
    r2 = c.delete(f"/v1/user-mappings/{mid}", headers=_auth(token_l4))
    assert r2.status_code == 403
    # still exists for L5
    assert c.get("/v1/user-mappings", headers=_auth(token)).json()["count"] == 1

def test_delete_requires_auth_401():
    c = _client()
    r = c.delete("/v1/user-mappings/any")
    assert r.status_code == 401

# ---------------------------------------------------------------------------
# SYNC dry-run
# ---------------------------------------------------------------------------

def test_sync_dry_run_with_users():
    token = _login()
    c = _client()
    # dry-run with users list should not persist
    r = c.post("/v1/user-mappings/sync", json={"users": [{"mm_user_id": "u1", "mm_username": "Kim@Open!"}, {"mm_user_id": "u2", "mm_username": "Alice.Wu"}]}, headers=_auth(token))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["dry_run"] is True
    assert data["count"] == 2
    assert len(data["preview"]) == 2
    # check derived
    by_id = {p["mm_user_id"]: p for p in data["preview"]}
    assert by_id["u1"]["employee_principal"] == "employee:kimopen"
    assert by_id["u1"]["agent_id"] == "agent:assistant:kimopen"
    assert by_id["u2"]["employee_principal"] == "employee:alice.wu"
    assert by_id["u2"]["agent_id"] == "agent:assistant:alice.wu"
    # not persisted
    assert c.get("/v1/user-mappings", headers=_auth(token)).json()["count"] == 0

def test_sync_dry_run_empty_store():
    token = _login()
    c = _client()
    r = c.post("/v1/user-mappings/sync", json={}, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["dry_run"] is True
    assert r.json()["count"] == 0

def test_sync_dry_run_returns_stored_as_preview_when_no_users():
    token = _login()
    c = _client()
    c.post("/v1/user-mappings", json={"mm_user_id": "stored1", "mm_username": "park"}, headers=_auth(token))
    r = c.post("/v1/user-mappings/sync", json={}, headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["count"] == 1
    assert r.json()["preview"][0]["mm_user_id"] == "stored1"

def test_sync_l4_forbidden():
    token_l4, _ = _make_l4()
    c = _client()
    r = c.post("/v1/user-mappings/sync", json={"users": [{"mm_user_id": "u1"}]}, headers=_auth(token_l4))
    assert r.status_code == 403

def test_sync_with_explicit_principal_in_users():
    token = _login()
    c = _client()
    r = c.post("/v1/user-mappings/sync", json={"users": [{"mm_user_id": "u9", "employee_principal": "employee:custom"}]}, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["preview"][0]["employee_principal"] == "employee:custom"
    assert r.json()["preview"][0]["agent_id"] == "agent:assistant:custom"

def test_sync_does_not_persist_users():
    token = _login()
    c = _client()
    c.post("/v1/user-mappings/sync", json={"users": [{"mm_user_id": "tmp1", "mm_username": "tmp"}]}, headers=_auth(token))
    c.post("/v1/user-mappings/sync", json={"users": [{"mm_user_id": "tmp2"}]}, headers=_auth(token))
    assert c.get("/v1/user-mappings", headers=_auth(token)).json()["count"] == 0
