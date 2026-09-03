"""Admin setup wizard + ACP/MCP config API tests (sqlite file DB, non-production)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"
TEST_DB = "/tmp/oaos_setup_acp_mcp_test.db"

os.environ["OAOS_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
for k in ("OAOS_DATABASE_URL", "OAOS_CP_HERMES_BASE_URL", "HERMES_BASE_URL"):
    os.environ.pop(k, None)


def _load(name: str, filename: str, bare_alias: str | None = None):
    added = False
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
        added = True
    try:
        spec = importlib.util.spec_from_file_location(name, str(BACKEND / filename))
        mod = importlib.util.module_from_spec(spec)  # type: ignore
        sys.modules[name] = mod
        if bare_alias and bare_alias not in sys.modules:
            sys.modules[bare_alias] = mod
        spec.loader.exec_module(mod)  # type: ignore
        return mod
    finally:
        if added and str(BACKEND) in sys.path:
            sys.path.remove(str(BACKEND))


auth_mod = _load("setup_auth_mod", "auth.py", bare_alias="auth")
_setup_mod = _load("setup_setup_mod", "setup.py")
_acp_mod = _load("setup_acp_mod", "acp_config.py")
_mcp_mod = _load("setup_mcp_mod", "mcp_config.py")
_app_mod = _load("setup_app_mod", "app.py")
app = _app_mod.app


def _router_mod(qname: str, fallback):
    mod = sys.modules.get(qname)
    return mod if mod is not None else fallback


@pytest.fixture()
def client(tmp_path):
    db_file = tmp_path / "oaos_setup_test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_file}"
    for qn, fb in (("admin_console.backend.setup", _setup_mod),
                   ("admin_console.backend.acp_config", _acp_mod),
                   ("admin_console.backend.mcp_config", _mcp_mod)):
        m = _router_mod(qn, fb)
        try:
            if getattr(m, "_db_engine", None) is not None:
                m._db_engine.dispose()
        except Exception:
            pass
        m._db_engine = None
        for attr in ("_inmem", "_inmem_completed"):
            if hasattr(m, attr):
                setattr(m, attr, None)
    auth_mod.clear_users()
    c = TestClient(app)
    yield c
    auth_mod.clear_users()


def _login(c: TestClient, email="admin@openit.co.kr", password="Admin123!") -> str:
    r = c.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_openapi_has_new_prefixes(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert any(p.startswith("/v1/setup") for p in paths)
    assert any(p.startswith("/v1/acp") for p in paths)
    assert any(p.startswith("/v1/mcp") for p in paths)


def test_setup_status_public_first_run(client):
    r = client.get("/v1/setup/status")
    assert r.status_code == 200
    body = r.json()
    assert body["first_run"] is True
    assert body["setup_completed"] is False
    assert "has_admin" in body


def test_setup_checks_requires_l5(client):
    r = client.post("/v1/setup/checks", json={})
    assert r.status_code == 401


def test_setup_checks_structure(client):
    tok = _login(client)
    r = client.post("/v1/setup/checks", json={}, headers=_h(tok))
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"db", "redis", "hermes"}
    assert body["db"]["ok"] is True  # sqlite file DB
    assert "api_key" not in json.dumps(body).lower()


def test_setup_complete_flow(client):
    tok = _login(client)
    r = client.post("/v1/setup/complete", headers=_h(tok))
    assert r.status_code == 200
    assert r.json()["setup_completed"] is True
    r2 = client.get("/v1/setup/status")
    assert r2.json()["first_run"] is False


def test_acp_config_default_and_update(client):
    tok = _login(client)
    r = client.get("/v1/acp/config", headers=_h(tok))
    assert r.status_code == 200
    assert "hermes_base_url" in r.json()
    assert "api_key" not in json.dumps(r.json()).lower().replace("api_key_set", "")
    r2 = client.put("/v1/acp/config", json={"hermes_base_url": "not-a-url"}, headers=_h(tok))
    assert r2.status_code == 422
    r3 = client.put("/v1/acp/config", json={"hermes_base_url": "http://127.0.0.1:8001", "hermes_model": "qwen2.5", "acp_enabled": False}, headers=_h(tok))
    assert r3.status_code == 200, r3.text
    assert r3.json()["source"] == "db"
    r4 = client.get("/v1/acp/config", headers=_h(tok))
    assert r4.json()["hermes_model"] == "qwen2.5"


def test_acp_test_unreachable(client):
    tok = _login(client)
    r = client.post("/v1/acp/test", json={"hermes_base_url": "http://127.0.0.1:9"}, headers=_h(tok))
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_mcp_crud_flow(client):
    tok = _login(client)
    r = client.get("/v1/mcp/servers", headers=_h(tok))
    assert r.json()["count"] == 0
    bad = client.post("/v1/mcp/servers", json={"name": "x", "transport": "grpc"}, headers=_h(tok))
    assert bad.status_code == 422
    r2 = client.post("/v1/mcp/servers", json={"name": "outline", "transport": "streamable-http", "url": "https://ol.oaos.cloud/mcp", "headers": {"Authorization": "Bearer s3cr3t"}}, headers=_h(tok))
    assert r2.status_code == 201, r2.text
    dup = client.post("/v1/mcp/servers", json={"name": "outline", "transport": "stdio", "command": "x"}, headers=_h(tok))
    assert dup.status_code == 409
    r3 = client.get("/v1/mcp/servers", headers=_h(tok))
    srv = r3.json()["servers"][0]
    assert srv["name"] == "outline"
    assert "s3cr3t" not in json.dumps(r3.json())
    assert srv["headers_set"] == ["Authorization"]
    r4 = client.put("/v1/mcp/servers/outline", json={"name": "outline", "transport": "stdio", "command": "outline-mcp"}, headers=_h(tok))
    assert r4.status_code == 200
    r5 = client.post("/v1/mcp/servers/outline/test", headers=_h(tok))
    assert r5.status_code == 200
    assert r5.json()["ok"] is None  # stdio: config-only
    r6 = client.delete("/v1/mcp/servers/outline", headers=_h(tok))
    assert r6.status_code == 200
    assert r6.json()["count"] == 0
    r7 = client.delete("/v1/mcp/servers/outline", headers=_h(tok))
    assert r7.status_code == 404


def test_setup_effective_masks_secrets(client, monkeypatch):
    tok = _login(client)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://oaos:s3cr3t-pw@127.0.0.1:5432/oaos")
    monkeypatch.setenv("REDIS_URL", "redis://:r3d1s-pw@127.0.0.1:6380/2")
    monkeypatch.setenv("OAOS_CP_HERMES_BASE_URL", "http://127.0.0.1:8001")
    monkeypatch.setenv("OAOS_CP_HERMES_MODEL", "qwen2.5")
    r = client.get("/v1/setup/effective", headers=_h(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["db"]["user"] == "oaos"
    assert body["db"]["host"] == "127.0.0.1"
    assert body["db"]["database"] == "oaos"
    assert body["redis"]["port"] == 6380
    assert body["redis"]["db"] == 2
    assert body["hermes"]["base_url"] == "http://127.0.0.1:8001"
    dumped = json.dumps(body)
    assert "s3cr3t-pw" not in dumped
    assert "r3d1s-pw" not in dumped


def test_setup_effective_requires_auth(client):
    r = client.get("/v1/setup/effective")
    assert r.status_code == 401
