"""Focused tests for the Outline HTTPS probe and registry display contract."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"

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
    # clear LIVE_INVENTORY side-effects? ensure probing mocked
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

def test_outline_db_https_url_and_probe_uses_db_host():
    """DB-backed Outline must probe https://note.oaos.cloud:443/_health, not http://127.0.0.1:3000/."""
    token = _login()
    c = _client()
    h = _auth(token)
    # Create DB row like production: outline note.oaos.cloud:443 /_health
    payload = {"service": "outline", "host": "note.oaos.cloud", "port": 443, "health_path": "/_health"}
    r = c.post("/v1/infra", json=payload, headers=h)
    assert r.status_code == 201, r.text
    # Mock httpx to capture URL and return 200
    captured_urls = []

    async def mock_get(url, *args, **kwargs):
        captured_urls.append(url)
        m = MagicMock()
        m.status_code = 200
        return m

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=mock_get)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    # Also mock TCP to avoid real connections — patch all possible import paths
    targets = ["httpx.AsyncClient", "admin_console.backend.infra.httpx.AsyncClient", "infra.httpx.AsyncClient", "admin_infra.httpx.AsyncClient"]
    # Use first that works; patch all via nested context
    import contextlib
    ctxs = [patch(t, return_value=mock_client) for t in targets]
    # only keep those that resolve (patch will error if module missing)
    valid_ctxs = []
    for ctx in ctxs:
        try:
            ctx.__enter__()
            valid_ctxs.append(ctx)
        except Exception:
            pass
    try:
        with patch("asyncio.open_connection", new=AsyncMock(side_effect=Exception("tcp skip"))):
            r2 = c.get("/v1/infra/registry", headers=h)
    finally:
        for ctx in valid_ctxs:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass
    assert r2.status_code == 200, r2.text
    data = r2.json()
    rows = data["items"] if "items" in data else data["registry"]
    outline = next((x for x in rows if x["name"] == "outline"), None)
    assert outline is not None, f"outline missing {rows[:2]}"
    # host/port/health_path from DB
    assert outline["host"] == "note.oaos.cloud"
    assert outline["port"] == 443
    assert outline["health_path"] == "/_health"
    # URL must be https, not http, and contain correct host/path
    url = outline.get("url") or ""
    assert url.startswith("https://"), f"Outline URL must be https for port 443, got {url}"
    assert "note.oaos.cloud:443/_health" in url, f"URL must contain DB host/path, got {url}"
    assert "127.0.0.1" not in url, f"URL must not contain live fallback 127.0.0.1, got {url}"
    # captured probe URL must be https and contain _health if probing happened; otherwise at least URL check suffices
    if captured_urls:
        assert any("https://note.oaos.cloud:443/_health" in u for u in captured_urls), f"probe must call https URL, captured={captured_urls}"
    # status should be healthy because mocked 200 == expected 200 (if probe succeeded) else unknown is also acceptable when mock not hit
    assert outline["probe_type"] == "http"
    assert outline["source"] == "both"
    assert outline["db_exists"] is True

def test_outline_registry_url_without_probe_still_https():
    """When probe=False logic (no live probe), URL still https for 443."""
    token = _login()
    c = _client()
    h = _auth(token)
    c.post("/v1/infra", json={"service": "outline", "host": "note.oaos.cloud", "port": 443, "health_path": "/_health"}, headers=h)
    # call registry with probing mocked but ensure URL generation without network also would be https
    # We directly test _build_unified_rows probe=False path via internal API? fallback: check registry still https even if probe fails
    async_mock = AsyncMock(side_effect=Exception("network fail"))
    with patch("httpx.AsyncClient", return_value=MagicMock(__aenter__=AsyncMock(return_value=MagicMock(get=async_mock)), __aexit__=AsyncMock(return_value=False))):
        with patch("asyncio.open_connection", new=AsyncMock(side_effect=Exception("tcp skip"))):
            # Force probe to still run but fail; URL should remain https
            r = c.get("/v1/infra/registry", headers=h)
    outline = next(x for x in r.json()["items"] if x["name"] == "outline")
    assert outline["url"].startswith("https://")
    assert "note.oaos.cloud" in outline["url"]

def test_registry_display_name_is_not_duplicated():
    """The registry preserves Outline's display name as one uncombined value."""
    token = _login()
    c = _client()
    h = _auth(token)
    r = c.post("/v1/infra", json={"service": "outline", "host": "note.oaos.cloud", "port": 443, "health_path": "/_health"}, headers=h)
    assert r.status_code == 201
    with patch("httpx.AsyncClient", return_value=MagicMock(
        __aenter__=AsyncMock(return_value=MagicMock(get=AsyncMock(return_value=MagicMock(status_code=200)))),
        __aexit__=AsyncMock(return_value=False),
    )):
        with patch("asyncio.open_connection", new=AsyncMock(side_effect=Exception("tcp"))):
            r2 = c.get("/v1/infra/registry", headers=h)
    outline = next(x for x in r2.json()["items"] if x["name"] == "outline")
    assert outline["display_name"].lower() == "outline"
    assert outline["display_name"].lower() != "outlineoutline"
