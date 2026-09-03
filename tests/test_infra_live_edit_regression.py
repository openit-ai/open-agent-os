"""Regression: live-only Infra row edit must not 404 — Update must POST/create canonical DB service.

- Editing live_outline (source=live, id=live_outline) must create DB entry, not 404
- Dedicated upsert POST /v1/infra/upsert is idempotent
- Existing DB PATCH still works
- L4 write blocked (403 before 404)
- Unified registry keeps probe_type/category, no secrets
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"

import importlib.util


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


# Use isolated names to avoid polluting session; but we need to load via canonical infra logic
# Prefer reusing already-loaded infra if present, else load
try:
    import admin_console.backend.infra as _canon_infra  # type: ignore
    infra_mod = _canon_infra
    import admin_console.backend.auth as _canon_auth  # type: ignore
    auth_mod = _canon_auth
    import admin_console.backend.app as _canon_app  # type: ignore
    app_mod = _canon_app
    admin_app = app_mod.app
except Exception:
    auth_mod = _load_admin_module("admin_auth_liveedit", "auth.py", bare_alias="auth")
    infra_mod = _load_admin_module("admin_infra_liveedit", "infra.py", bare_alias="infra")
    app_mod = _load_admin_module("admin_app_liveedit", "app.py")
    if str(BACKEND) in sys.path:
        sys.path.remove(str(BACKEND))
    admin_app = app_mod.app


@pytest.fixture(autouse=True)
def isolate():
    # clear canonical and any bare-alias copies to avoid cross-suite leak
    for mod_name in ("admin_console.backend.infra", "infra", "admin_infra", "admin_infra_liveedit", "admin_auth", "admin_auth_liveedit", "admin_console.backend.auth", "auth"):
        m = sys.modules.get(mod_name)
        if m is not None:
            try:
                if hasattr(m, "clear_users"):
                    m.clear_users()
            except Exception:
                pass
            try:
                if hasattr(m, "clear_services"):
                    m.clear_services()
            except Exception:
                pass
    # also ensure our primary refs are cleared
    try:
        auth_mod.clear_users()
    except Exception:
        pass
    try:
        infra_mod.clear_services()
    except Exception:
        pass
    yield
    for mod_name in ("admin_console.backend.infra", "infra", "admin_infra", "admin_infra_liveedit", "admin_auth", "admin_auth_liveedit", "admin_console.backend.auth", "auth"):
        m = sys.modules.get(mod_name)
        if m is not None:
            try:
                if hasattr(m, "clear_users"):
                    m.clear_users()
            except Exception:
                pass
            try:
                if hasattr(m, "clear_services"):
                    m.clear_services()
            except Exception:
                pass
    try:
        auth_mod.clear_users()
    except Exception:
        pass
    try:
        infra_mod.clear_services()
    except Exception:
        pass


def _client():
    return TestClient(admin_app)


def _login(email="admin@openit.co.kr", password="Admin123!"):
    c = _client()
    r = c.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def test_live_only_patch_creates_canonical_instead_of_404():
    token = _login()
    c = _client()
    h = _h(token)
    # ensure unified has live_outline as live-only
    # Mock probe to avoid network
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mc = AsyncMock()
    mc.get = AsyncMock(return_value=mock_resp)
    mc.__aenter__ = AsyncMock(return_value=mc)
    mc.__aexit__ = AsyncMock(return_value=False)
    with patch("httpx.AsyncClient", return_value=mc):
        # Also patch asyncio.open_connection for tcp probes to avoid outbound
        with patch("asyncio.open_connection", new=AsyncMock(side_effect=Exception("tcp skip"))):
            r = c.get("/v1/infra/unified", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    rows = data["items"]
    live_outline = next((x for x in rows if x["id"] == "live_outline"), None)
    assert live_outline is not None, f"live_outline missing in unified: {[x['id'] for x in rows]}"
    assert live_outline["source"] == "live"
    assert live_outline["db_exists"] is False
    assert live_outline["probe_type"] in ("http", "tcp")
    assert live_outline["category"] == "knowledge"
    assert "password" not in str(live_outline).lower()
    assert "dsn" not in str(live_outline).lower()

    # Frontend minimal safe flow: PATCH live_outline should create canonical DB service, not 404
    # The backend patch alias for live_ now upserts
    payload = {"service": "outline", "host": "10.0.0.99", "port": 3999, "health_path": "/api/health"}
    r2 = c.patch("/v1/infra/live_outline", json=payload, headers=h)
    assert r2.status_code == 200, f"live patch should succeed via upsert fallback, got {r2.status_code} {r2.text}"
    j2 = r2.json()
    assert j2["name"] == "outline" or j2.get("service") == "outline"
    assert j2["host"] == "10.0.0.99"
    assert j2["port"] == 3999
    # Now unified should show source=both and db_exists True for outline
    with patch("httpx.AsyncClient", return_value=mc):
        with patch("asyncio.open_connection", new=AsyncMock(side_effect=Exception("tcp skip"))):
            r3 = c.get("/v1/infra/unified", headers=h)
    rows2 = r3.json()["items"]
    outline_row = next((x for x in rows2 if x["name"] == "outline"), None)
    assert outline_row is not None
    assert outline_row["db_exists"] is True
    # subsequent PATCH on the canonical id should still work
    canonical_id = j2["id"]
    r4 = c.patch(f"/v1/infra/{canonical_id}", json={"host": "10.0.0.100"}, headers=h)
    assert r4.status_code == 200
    assert r4.json()["host"] == "10.0.0.100"


def test_dedicated_upsert_idempotent():
    token = _login()
    c = _client()
    h = _h(token)
    payload = {"service": "redis", "host": "127.0.0.99", "port": 6380, "health_path": "/"}
    r1 = c.post("/v1/infra/upsert", json=payload, headers=h)
    assert r1.status_code == 200, r1.text
    id1 = r1.json()["id"]
    payload2 = {"service": "redis", "host": "127.0.0.99", "port": 6381, "health_path": "/health"}
    r2 = c.post("/v1/infra/upsert", json=payload2, headers=h)
    assert r2.status_code == 200, f"second upsert failed {r2.status_code} {r2.text}"
    # should update same canonical row, not duplicate by name — check via both alias endpoints
    for endpoint in ("/v1/infra", "/v1/infra/services"):
        c2 = c.get(endpoint, headers=h)
        if c2.status_code != 200:
            continue
        body = c2.json()
        items = body["items"] if isinstance(body, dict) and "items" in body else (body if isinstance(body, list) else [])
        redis_rows = [x for x in items if (x.get("name") or x.get("service")) == "redis"]
        if redis_rows:
            assert len(redis_rows) == 1, f"upsert must be idempotent by name at {endpoint}, got {len(redis_rows)} redis rows: {redis_rows} full={items}"
            assert redis_rows[0]["port"] == 6381, f"upsert should update port, got {redis_rows[0]}"
            assert r2.json()["id"] == id1 or redis_rows[0]["id"] == id1
            return
    # fallback: fetched empty, treat as failure with diagnostics
    c2 = c.get("/v1/infra", headers=h)
    body = c2.json() if c2.status_code == 200 else {}
    assert False, f"no redis rows after upsert; upsert1={r1.json()} upsert2={r2.json()} list={body}"


def test_l4_cannot_write_live_or_db():
    token_l5 = _login()
    c = _client()
    # create L4
    c.post("/v1/auth/register", json={"email": "l4_live@test.co.kr", "password": "Password123!", "display_name": "L4", "role": "L4"}, headers=_h(token_l5))
    r = c.post("/v1/auth/login", json={"email": "l4_live@test.co.kr", "password": "Password123!"})
    token_l4 = r.json()["access_token"]
    h4 = _h(token_l4)
    # L4 patch live should be 403 before 404
    r2 = c.patch("/v1/infra/live_outline", json={"service": "outline", "host": "1.1.1.1", "port": 3000}, headers=h4)
    assert r2.status_code == 403
    r3 = c.post("/v1/infra/upsert", json={"service": "outline", "host": "1.1.1.1", "port": 3000}, headers=h4)
    assert r3.status_code == 403
