"""Admin P2 write surfaces — quota/embedding/secrets/feature-flags API tests (sqlite file DB, non-production)."""
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

os.environ["OAOS_ENV"] = "test"
os.environ["DATABASE_URL"] = "/tmp/oaos_p2_surfaces_test.db"
for k in ("OAOS_DATABASE_URL", "OAOS_CP_HERMES_BASE_URL", "HERMES_BASE_URL",
          "OAOS_EMBED_API_URL", "OAOS_EMBEDDING_API_URL", "OLLAMA_API_URL",
          "OAOS_EMBED_MODEL", "OAOS_EMBED_DIM"):
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


auth_mod = _load("p2_auth_mod", "auth.py", bare_alias="auth")
_app_mod = _load("p2_app_mod", "app.py")
app = _app_mod.app

P2_QNAMES = (
    "admin_console.backend.quota_admin",
    "admin_console.backend.embedding_config",
    "admin_console.backend.secrets_admin",
    "admin_console.backend.feature_flags",
)


@pytest.fixture()
def client(tmp_path):
    db_file = tmp_path / "oaos_p2_test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_file}"
    for qn in P2_QNAMES:
        m = sys.modules.get(qn)
        assert m is not None, f"router module not mounted: {qn}"
        try:
            if getattr(m, "_db_engine", None) is not None:
                m._db_engine.dispose()
        except Exception:
            pass
        m._db_engine = None
        if hasattr(m, "_inmem"):
            setattr(m, "_inmem", None)
    auth_mod.clear_users()
    c = TestClient(app)
    yield c
    auth_mod.clear_users()


def _login(c: TestClient, email="admin@openit.co.kr", password="Admin123!") -> str:
    r = c.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(c: TestClient) -> dict:
    return {"Authorization": f"Bearer {_login(c)}"}


def test_openapi_has_p2_prefixes(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert any(p.startswith("/v1/quota") for p in paths)
    assert any(p.startswith("/v1/embedding") for p in paths)
    assert any(p.startswith("/v1/secrets") for p in paths)
    assert any(p.startswith("/v1/feature-flags") for p in paths)


def test_quota_limits_default_and_override_roundtrip(client):
    h = _auth(client)
    r = client.get("/v1/quota/limits?tenant_id=acme", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    # enforcement defaults mirrored, enforcement path untouched
    assert body["daily_limit"] == 100
    assert body["per_minute_limit"] == 10
    assert body["defaults"] == {"daily_limit": 100, "per_minute_limit": 10}
    assert body["overridden"] is False
    assert body["enforcement"] == "unchanged"
    # validation
    bad = client.put("/v1/quota/limits", headers=h, json={"tenant_id": "acme", "daily_limit": 0})
    assert bad.status_code == 422, bad.text
    empty = client.put("/v1/quota/limits", headers=h, json={"tenant_id": "acme"})
    assert empty.status_code == 422, empty.text
    # override stored in NEW key only
    r2 = client.put("/v1/quota/limits", headers=h,
                    json={"tenant_id": "acme", "daily_limit": 500, "per_minute_limit": 25})
    assert r2.status_code == 200, r2.text
    assert r2.json()["source"] == "db"
    assert r2.json()["enforcement"] == "unchanged"
    r3 = client.get("/v1/quota/limits?tenant_id=acme", headers=h)
    assert r3.json()["daily_limit"] == 500
    assert r3.json()["per_minute_limit"] == 25
    assert r3.json()["overridden"] is True
    # other tenant unaffected
    r4 = client.get("/v1/quota/limits?tenant_id=default", headers=h)
    assert r4.json()["daily_limit"] == 100


def test_quota_usage_read_only(client):
    h = _auth(client)
    r = client.get("/v1/quota/usage?tenant_id=acme", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == "acme"
    assert body["effective_limits"]["daily_limit"] == 100
    assert body["enforcement"] == "unchanged"
    assert "usage" in body and "usage_source" in body
    assert client.get("/v1/quota/usage", headers=h).status_code == 200
    assert client.get("/v1/quota/limits").status_code == 401
    assert client.get("/v1/quota/usage").status_code == 401


def test_embedding_config_roundtrip_and_guards(client):
    h = _auth(client)
    r = client.get("/v1/embedding/config", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] and body["api_url"] and body["dim"]
    assert body["restart_required"] is False  # env source initially
    assert body["applied"] is True
    bad = client.put("/v1/embedding/config", headers=h, json={"api_url": "http://x:8000/vectors"})
    assert bad.status_code == 422, bad.text
    bad2 = client.put("/v1/embedding/config", headers=h, json={"provider": "chromadb"})
    assert bad2.status_code == 422, bad2.text
    r2 = client.put("/v1/embedding/config", headers=h,
                    json={"provider": "ollama", "model": "nomic-embed-text", "dim": 768,
                          "api_url": "http://127.0.0.1:11434"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["source"] == "db"
    assert r2.json()["restart_required"] is True
    assert r2.json()["applied"] is False
    r3 = client.get("/v1/embedding/config", headers=h)
    assert r3.json()["model"] == "nomic-embed-text"
    assert r3.json()["dim"] == 768


def test_secrets_status_never_returns_values(client, monkeypatch):
    monkeypatch.setenv("ADMIN_JWT_SECRET", "test-admin-jwt-secret-value-0123456789")
    monkeypatch.delenv("AUDIT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("OAOS_AUDIT_SIGNING_KEY", raising=False)
    h = _auth(client)
    r = client.get("/v1/secrets/status", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 4
    names = [i["name"] for i in body["items"]]
    assert names == ["JWT_SIGNING_KEY", "AUDIT_SIGNING_KEY", "ADMIN_JWT_SECRET", "OAOS_ENCRYPTION_KEY"]
    dumped = json.dumps(body)
    assert "test-admin-jwt-secret-value-0123456789" not in dumped
    for item in body["items"]:
        assert set(item.keys()) == {"name", "configured", "length", "source_env", "rotation_needed", "reason"}
    admin_item = next(i for i in body["items"] if i["name"] == "ADMIN_JWT_SECRET")
    assert admin_item["configured"] is True
    assert admin_item["length"] == len("test-admin-jwt-secret-value-0123456789")
    assert admin_item["source_env"] == "ADMIN_JWT_SECRET"
    audit_item = next(i for i in body["items"] if i["name"] == "AUDIT_SIGNING_KEY")
    assert audit_item["rotation_needed"] is True
    # guide: steps + checklist, no execution endpoint
    g = client.get("/v1/secrets/rotation-guide", headers=h)
    assert g.status_code == 200, g.text
    assert len(g.json()["steps"]) >= 3
    assert len(g.json()["checklist"]) >= 3
    assert g.json()["executes_rotation"] is False
    assert client.post("/v1/secrets/rotate", headers=h).status_code in (404, 405)
    assert client.get("/v1/secrets/status").status_code == 401


def test_feature_flags_toggle_roundtrip(client):
    h = _auth(client)
    r = client.get("/v1/feature-flags", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runtime_wired"] is False
    assert any(f["name"] == "maintenance_banner" for f in body["flags"])
    assert all("enabled" in f and "default" in f for f in body["flags"])
    bad = client.put("/v1/feature-flags", headers=h, json={"name": "Bad-Name!", "enabled": True})
    assert bad.status_code == 422, bad.text
    r2 = client.put("/v1/feature-flags", headers=h, json={"name": "maintenance_banner", "enabled": True})
    assert r2.status_code == 200, r2.text
    assert r2.json()["enabled"] is True
    assert r2.json()["overridden"] is True
    assert r2.json()["runtime_wired"] is False
    r3 = client.get("/v1/feature-flags", headers=h)
    mb = next(f for f in r3.json()["flags"] if f["name"] == "maintenance_banner")
    assert mb["enabled"] is True and mb["overridden"] is True
    # toggle back
    client.put("/v1/feature-flags", headers=h, json={"name": "maintenance_banner", "enabled": False})
    r4 = client.get("/v1/feature-flags", headers=h)
    assert next(f for f in r4.json()["flags"] if f["name"] == "maintenance_banner")["enabled"] is False


def test_p2_keys_isolated_and_enforcement_untouched(client):
    h = _auth(client)
    client.put("/v1/quota/limits", headers=h, json={"tenant_id": "iso", "daily_limit": 777})
    client.put("/v1/embedding/config", headers=h, json={"model": "bge-m3:latest"})
    client.put("/v1/feature-flags", headers=h, json={"name": "beta_llm_dashboard", "enabled": True})
    from sqlalchemy import create_engine, text
    eng = create_engine(os.environ["DATABASE_URL"])
    with eng.connect() as conn:
        keys = sorted(row[0] for row in conn.execute(text("SELECT key FROM admin_settings")).fetchall())
        vals = {row[0]: row[1] for row in conn.execute(text("SELECT key, value FROM admin_settings")).fetchall()}
    assert "quota_overrides" in keys
    assert "embedding_config" in keys
    assert "feature_flags" in keys
    assert "mm_config" not in keys
    assert "outline_config" not in keys
    # overrides live ONLY in the new JSON key
    assert json.loads(vals["quota_overrides"])["iso"]["daily_limit"] == 777
    # runtime enforcement defaults still hardcoded 100/10
    src = (ROOT / "packages" / "agent-runtime" / "agent_runtime" / "llm_runtime.py").read_text()
    assert "return 100, 10" in src
