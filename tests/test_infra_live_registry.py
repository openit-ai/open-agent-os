"""Regression: unified Infra Registry edit for live-only rows — no 404."""
from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path

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

def _reset_infra_state():
    """Reset the service store (and DB handles) of *every* loaded admin infra module.

    app.py resolves its infra sibling with `_load_admin_sibling("infra")`, which
    reuses whatever module object already occupies `sys.modules["infra"]`. When
    another test file loaded that name first, it is NOT the module this file holds
    in `infra_mod`, so clearing only `infra_mod` left rows written by these tests
    (e.g. the edited `live_outline` host) visible to the app and to later tests.
    """
    seen: set[int] = set()
    for name, mod in list(sys.modules.items()):
        if mod is None or "infra" not in name or id(mod) in seen:
            continue
        if not hasattr(mod, "clear_services"):
            continue
        seen.add(id(mod))
        engine = getattr(mod, "_db_engine", None)
        if engine is not None:
            with contextlib.suppress(Exception):
                engine.dispose()
        mod._db_engine = None
        mod._db_session_factory = None
        with contextlib.suppress(Exception):
            mod.clear_services()


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    auth_mod.clear_users()
    _reset_infra_state()
    yield
    auth_mod.clear_users()
    _reset_infra_state()

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
    # Arbitrary throwaway labels for "value written first" and "value written again".
    # They are NOT deployed addresses and mean nothing outside this test; they only
    # let the assertions tell the first write apart from the overwrite.
    first_host, first_port = "127.0.0.2", 3002
    second_host, second_port = "127.0.0.3", 3003
    payload = {"service": "outline", "host": first_host, "port": first_port, "health_path": "/"}
    r = c.patch("/v1/infra/live_outline", json=payload, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["name"] == "outline"
    assert d["host"] == first_host
    assert d["id"].startswith("infra_")
    # Second PATCH must update the row this test just created, not insert another one.
    r2 = c.patch("/v1/infra/live_outline", json={"host": second_host, "port": second_port}, headers=h)
    assert r2.status_code == 200, r2.text
    assert r2.json()["host"] == second_host
    assert r2.json()["id"] == d["id"], "the second edit must reuse the row, not create a new one"
    r3 = c.get("/v1/infra/registry", headers=h)
    outlines = [x for x in r3.json()["items"] if x["name"] == "outline"]
    assert len(outlines) == 1
    assert outlines[0]["source"] == "both"
    assert outlines[0]["host"] == second_host

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
