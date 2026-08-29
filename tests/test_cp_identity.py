"""H1 — Control Plane identity tests (v1.7.1 I-H1-1..3)."""
import os
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "control-plane"))

TEST_KEY = "test-signing-key-32bytes-long-enough!"
os.environ["OAOS_SIGNING_KEY"] = TEST_KEY

from fastapi.testclient import TestClient
from control_plane.app import app
from control_plane.auth import issue_user_jwt
from control_plane.session import session_store

def _jwt(sub="employee:kim", tenant="acme", ttl=3600, extra=None):
    return issue_user_jwt(sub, tenant_id=tenant, ttl_seconds=ttl, signing_key=TEST_KEY, extra=extra)

def _auth(jwt):
    return {"Authorization": f"Bearer {jwt}"}

def setup_function(fn):
    # clear session store before each test
    try:
        store = session_store
        if hasattr(store, "_store"):
            store._store.clear()
        elif hasattr(store, "_fallback_store") and hasattr(store._fallback_store, "_store"):
            store._fallback_store._store.clear()
    except Exception:
        pass

def test_plaintext_rejected_in_prod(monkeypatch):
    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.setenv("OAOS_SIGNING_KEY", TEST_KEY)
    c = TestClient(app)
    r = c.post("/v1/sessions", json={"tenant_id": "acme", "user_id": "employee:kim"}, headers={"X-User-Id": "employee:kim"})
    assert r.status_code == 401

def test_jwt_valid_accepted():
    jwt = _jwt()
    c = TestClient(app)
    r = c.post("/v1/sessions", json={"tenant_id": "acme", "user_id": "employee:kim"}, headers={"X-User-Id": "employee:kim", **_auth(jwt)})
    assert r.status_code == 200
    assert r.json()["agent_id"] == "agent:assistant:kim"

def test_sub_mismatch_401():
    jwt = _jwt(sub="employee:kim")
    c = TestClient(app)
    r = c.post("/v1/sessions", json={"tenant_id": "acme", "user_id": "employee:lee"}, headers={"X-User-Id": "employee:lee", **_auth(jwt)})
    assert r.status_code == 401
    assert "IDENTITY_MISMATCH" in r.text or "mismatch" in r.text.lower()

def test_expired_401():
    jwt = _jwt(ttl=-60)
    c = TestClient(app)
    r = c.post("/v1/sessions", json={"tenant_id": "acme", "user_id": "employee:kim"}, headers={"X-User-Id": "employee:kim", **_auth(jwt)})
    assert r.status_code == 401

def test_session_owner_isolation_with_jwt():
    jwt_kim = _jwt(sub="employee:kim", tenant="acme")
    jwt_lee = _jwt(sub="employee:lee", tenant="acme")
    c = TestClient(app)
    r = c.post("/v1/sessions", json={"tenant_id": "acme", "user_id": "employee:kim"}, headers={"X-User-Id": "employee:kim", **_auth(jwt_kim)})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    r2 = c.get(f"/v1/sessions/{sid}", headers={"X-User-Id": "employee:lee", **_auth(jwt_lee)})
    assert r2.status_code == 403
