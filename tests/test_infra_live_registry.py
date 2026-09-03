"""Regression: unified Infra Registry edit for live-only rows — no 404."""
from __future__ import annotations

import sys
from pathlib import Path

import importlib.util
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"

# Reuse already-loaded admin modules if present (test_admin_backend loads them first)
# to ensure shared _services dict and consistent DB handling; otherwise load fresh.
def _load_or_reuse(name: str, filename: str, bare_alias: str | None = None):
    if name in sys.modules:
        mod = sys.modules[name]
        if bare_alias and bare_alias not in sys.modules:
            sys.modules[bare_alias] = mod
        return mod
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location(name, str(BACKEND / filename))
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    sys.modules[name] = mod
    if bare_alias:
        sys.modules[bare_alias] = mod
    spec.loader.exec_module(mod)  # type: ignore
    return mod

auth_mod = _load_or_reuse("admin_auth", "auth.py", bare_alias="auth")
infra_mod = _load_or_reuse("admin_infra", "infra.py", bare_alias="infra")
# app module: reuse if already loaded, else load (admin_app from test_admin_backend)
if "admin_app" in sys.modules:
    app_mod = sys.modules["admin_app"]
else:
    app_mod = _load_or_reuse("admin_app", "app.py")
if str(BACKEND) in sys.path:
    try:
        sys.path.remove(str(BACKEND))
    except ValueError:
        pass
admin_app = app_mod.app

@pytest.fixture(autouse=True)
def isolate():
    auth_mod.clear_users()
    infra_mod.clear_services()
    yield
    auth_mod.clear_users()
    infra_mod.clear_services()

def _client():
    return TestClient(admin_app)

def _login(email="admin@openit.co.kr", password="Admin123!"):
    c = _client()
    r = c.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]

def _auth(token):
    return {"Authorization": f"Bearer {token}"}

def test_live_only_patch_upsert_no_404():
    """PATCH live_outline must not 404 — it should upsert/register into DB (user clicked Update)."""
    token = _login()
    c = _client()
    h = _auth(token)
    r = c.get("/v1/infra/registry", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    live_outline = next((x for x in items if x["id"] == "live_outline" or x["name"] == "outline"), None)
    assert live_outline is not None, f"outline not in registry: {items[:2]}"
    assert live_outline.get("source") == "live" or live_outline.get("db_exists") is False

    payload = {"service": "outline", "host": "127.0.0.1", "port": 3000, "health_path": "/"}
    r2 = c.post("/v1/infra", json=payload, headers=h)
    assert r2.status_code == 201, r2.text
    created_id = r2.json()["id"]
    assert created_id.startswith("infra_")
    assert r2.json()["name"] == "outline"

    r3 = c.get("/v1/infra/registry", headers=h)
    assert r3.status_code == 200
    items2 = r3.json()["items"]
    outline2 = next(x for x in items2 if x["name"] == "outline")
    assert outline2["source"] == "both"
    assert outline2["db_exists"] is True
    r4 = c.patch(f"/v1/infra/{created_id}", json={"host": "10.0.0.5", "port": 3001}, headers=h)
    assert r4.status_code == 200, r4.text
    assert r4.json()["host"] == "10.0.0.5"
    assert r4.json()["port"] == 3001

def test_live_patch_direct_upsert_when_no_prior_post():
    """Direct PATCH live_* id without prior POST should also register (backend upsert safety)."""
    token = _login()
    c = _client()
    h = _auth(token)
    payload = {"service": "outline", "host": "127.0.0.2", "port": 3002, "health_path": "/"}
    r = c.patch("/v1/infra/live_outline", json=payload, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["name"] == "outline"
    assert d["host"] == "127.0.0.2"
    assert d["id"].startswith("infra_")
    r2 = c.patch("/v1/infra/live_outline", json={"host": "127.0.0.3", "port": 3003}, headers=h)
    assert r2.status_code == 200, r2.text
    assert r2.json()["host"] == "127.0.0.3"
    r3 = c.get("/v1/infra/registry", headers=h)
    outlines = [x for x in r3.json()["items"] if x["name"] == "outline"]
    assert len(outlines) == 1
    assert outlines[0]["source"] == "both"

def test_db_row_patch_unchanged():
    """Existing DB rows still PATCH normally via their infra_* id."""
    token = _login()
    c = _client()
    h = _auth(token)
    r = c.post("/v1/infra", json={"service": "hermes", "host": "127.0.0.1", "port": 8642, "health_path": "/health"}, headers=h)
    assert r.status_code == 201
    sid = r.json()["id"]
    r2 = c.patch(f"/v1/infra/{sid}", json={"host": "10.10.10.10"}, headers=h)
    assert r2.status_code == 200
    assert r2.json()["host"] == "10.10.10.10"
    r3 = c.patch("/v1/infra/infra_nonexist123", json={"host": "1.1.1.1"}, headers=h)
    assert r3.status_code == 404

def test_live_registration_no_secrets_and_probe_metadata_preserved():
    """POST live registration stores only host/port/health_path; probe_type/category/url via live metadata."""
    token = _login()
    c = _client()
    h = _auth(token)
    c.post("/v1/infra", json={"service": "postgres", "host": "127.0.0.1", "port": 5432, "health_path": "/"}, headers=h)
    r = c.get("/v1/infra/registry", headers=h)
    pg = next(x for x in r.json()["items"] if x["name"] == "postgres")
    assert pg["probe_type"] == "tcp"
    assert pg["category"] == "datastore"
    assert pg["url"].startswith("tcp://")
    import json as _json
    dump = _json.dumps(pg)
    assert "password" not in dump.lower()
    assert "dsn" not in dump.lower()

def test_l4_cannot_register_live():
    """L5 required for live registration via PATCH or POST."""
    token_l5 = _login()
    c = _client()
    c.post("/v1/auth/register", json={"email": "l4b@test.co.kr", "password": "Password123!", "display_name": "L4B", "role": "L4"}, headers=_auth(token_l5))
    r = c.post("/v1/auth/login", json={"email": "l4b@test.co.kr", "password": "Password123!"})
    token_l4 = r.json()["access_token"]
    h4 = _auth(token_l4)
    r2 = c.patch("/v1/infra/live_outline", json={"service": "outline", "host": "1.1.1.1", "port": 3000}, headers=h4)
    assert r2.status_code == 403
    r3 = c.post("/v1/infra", json={"service": "outline", "host": "1.1.1.1", "port": 3000}, headers=h4)
    assert r3.status_code == 403
