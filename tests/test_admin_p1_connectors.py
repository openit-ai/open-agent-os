"""Admin P1 connector surfaces — notion/slack/oauth/smtp config API tests (sqlite file DB, non-production)."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"

os.environ["OAOS_ENV"] = "test"
os.environ["DATABASE_URL"] = "/tmp/oaos_p1_connectors_test.db"
for k in ("OAOS_DATABASE_URL", "OAOS_CP_HERMES_BASE_URL", "HERMES_BASE_URL",
          "NOTION_API_KEY", "NOTION_TOKEN", "SLACK_WEBHOOK_URL",
          "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
          "MS_CLIENT_ID", "MS_CLIENT_SECRET", "MICROSOFT_CLIENT_ID",
          "SMTP_HOST", "SMTP_PASSWORD"):
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


auth_mod = _load("p1_auth_mod", "auth.py", bare_alias="auth")
_app_mod = _load("p1_app_mod", "app.py")
app = _app_mod.app

P1_QNAMES = (
    "admin_console.backend.notion_config",
    "admin_console.backend.slack_config",
    "admin_console.backend.oauth_config",
    "admin_console.backend.smtp_config",
)


@pytest.fixture()
def client(tmp_path):
    db_file = tmp_path / "oaos_p1_test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_file}"
    for qn in P1_QNAMES:
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


def test_notion_config_roundtrip_write_only_key(client):
    h = _auth(client)
    r = client.get("/v1/notion/config", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["notion_api_url"]
    assert "api_key" not in r.json()
    r = client.put("/v1/notion/config", headers=h,
                   json={"notion_api_url": "https://api.notion.com", "api_key": "secret_notion_xyz"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["api_key_set"] is True
    assert "secret_notion_xyz" not in r.text
    r = client.get("/v1/notion/config", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["api_key_set"] is True
    assert "secret_notion_xyz" not in r.text


def test_notion_test_requires_key(client):
    h = _auth(client)
    r = client.post("/v1/notion/test", headers=h, json={})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    assert "api_key" in r.json()["error"]


def test_slack_webhook_validation_and_test_probe(client):
    h = _auth(client)
    r = client.put("/v1/slack/config", headers=h, json={"webhook_url": "http://evil.example/hook"})
    assert r.status_code == 422, r.text
    r = client.put("/v1/slack/config", headers=h,
                   json={"webhook_url": "https://hooks.slack.com/services/T/B/X", "channel": "#alerts"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["webhook_url_set"] is True
    assert body["channel"] == "#alerts"
    assert "hooks.slack.com/services" not in r.text
    r = client.post("/v1/slack/test", headers=h, json={})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False  # no env webhook; one-shot only


def test_oauth_env_only_and_prefs(client):
    h = _auth(client)
    r = client.get("/v1/oauth/config", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "google_redirect_uri" in body and "microsoft_redirect_uri" in body
    assert body["google_client_id_set"] is False
    assert "env" in body["note"].lower() or "DB" in body["note"]
    assert "GOOGLE_CLIENT_SECRET" not in r.text
    # secrets must be rejected at the API boundary (model extra=forbid)
    r = client.put("/v1/oauth/config", headers=h, json={"google_client_secret": "shh"})
    assert r.status_code == 422, r.text
    r = client.put("/v1/oauth/config", headers=h, json={"google_enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["google_enabled"] is True
    r = client.post("/v1/oauth/test", headers=h, json={})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    assert r.json()["providers"]["google"]["configured"] is False


def test_smtp_roundtrip_and_no_mail_test(client):
    h = _auth(client)
    r = client.put("/v1/smtp/config", headers=h,
                   json={"smtp_host": "127.0.0.1", "smtp_port": 1, "smtp_user": "u@example.com",
                         "smtp_password": "pw123", "use_starttls": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["smtp_password_set"] is True
    assert "pw123" not in r.text
    r = client.get("/v1/smtp/config", headers=h)
    assert r.json()["smtp_host"] == "127.0.0.1"
    assert r.json()["smtp_port"] == 1
    assert "pw123" not in r.text
    r = client.post("/v1/smtp/test", headers=h, json={})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False  # closed port: connection check only, never sends mail


def test_p1_keys_isolated_from_mm_outline(client):
    h = _auth(client)
    client.put("/v1/notion/config", headers=h, json={"api_key": "ntn_x"})
    client.put("/v1/slack/config", headers=h, json={"webhook_url": "https://hooks.slack.com/services/T/B/X"})
    client.put("/v1/smtp/config", headers=h, json={"smtp_host": "mail.example.com"})
    from sqlalchemy import create_engine, text
    eng = create_engine(os.environ["DATABASE_URL"])
    with eng.connect() as conn:
        keys = sorted(row[0] for row in conn.execute(text("SELECT key FROM admin_settings")).fetchall())
    assert "notion_config" in keys
    assert "slack_config" in keys
    assert "smtp_config" in keys
    assert "mm_config" not in keys
    assert "outline_config" not in keys
