"""H2 — EGW signed context tests (v1.7.1 I-H2-1..3)."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "execution-gateway"))
sys.path.insert(0, str(ROOT / "control-plane"))

TEST_KEY = "test-signing-key-32bytes-long-enough!"
os.environ["OAOS_SIGNING_KEY"] = TEST_KEY

from fastapi.testclient import TestClient
from execution_gateway.app import app
from execution_gateway.signed_context import issue_agent_context_jwt

def _jwt(tenant="acme", user="employee:kim", agent="agent:assistant:kim", sess="sess_xxx", tenant_override=None, **kw):
    tid = tenant_override if tenant_override else tenant
    return issue_agent_context_jwt(tenant_id=tid, user_id=user, agent_id=agent, session_id=sess, trace_id="trace_yyy", request_id="req_zzz", **kw)

def test_plaintext_rejected_in_prod(monkeypatch):
    monkeypatch.setenv("OAOS_ENV", "production")
    c = TestClient(app)
    r = c.post("/v1/execute", json={"tool": "gmail_search", "action": "READ", "resource": "gmail/user/kim/*"}, headers={"X-Agent-Context": '{"tenant_id":"acme","user_id":"employee:kim"}'})
    assert r.status_code == 401

def test_valid_jwt_accepted():
    jwt = _jwt()
    c = TestClient(app)
    r = c.post("/v1/execute", json={"tool": "gmail_search", "action": "READ", "resource": "gmail/user/kim/*"}, headers={"X-Agent-Context-JWT": jwt})
    assert r.status_code != 401, r.text

def test_tenant_mismatch_403():
    jwt = _jwt(tenant="acme")
    c = TestClient(app)
    r = c.post("/v1/execute", json={"tool": "gmail_search", "action": "READ", "resource": "gmail/user/kim/*"}, headers={"X-Agent-Context-JWT": jwt, "X-Tenant-Id": "evil"})
    assert r.status_code in (401, 403)

def test_expired_401():
    jwt = _jwt(ttl_seconds=-60)
    c = TestClient(app)
    r = c.post("/v1/execute", json={"tool": "gmail_search", "action": "READ", "resource": "gmail/user/kim/*"}, headers={"X-Agent-Context-JWT": jwt})
    assert r.status_code == 401

def test_tampered_payload_401():
    jwt = _jwt()
    tampered = jwt[:-4] + "abcd"
    c = TestClient(app)
    r = c.post("/v1/execute", json={"tool": "gmail_search", "action": "READ", "resource": "gmail/user/kim/*"}, headers={"X-Agent-Context-JWT": tampered})
    assert r.status_code == 401

def test_capability_session_binding(monkeypatch):
    import jose.jwt as jose_jwt
    jwt_ctx = _jwt(sess="sess_xxx")
    cap = jose_jwt.encode({"sub": "agent:assistant:kim", "session_id": "sess_other", "exp": 9999999999}, TEST_KEY, algorithm="HS256")
    c = TestClient(app)
    r = c.post("/v1/execute", json={"tool": "gmail_search", "action": "READ", "resource": "gmail/user/kim/*", "capability_token": cap}, headers={"X-Agent-Context-JWT": jwt_ctx})
    assert r.status_code == 403

def test_nonprod_plaintext_rejected_when_enforced(monkeypatch):
    monkeypatch.delenv("OAOS_ENV", raising=False)
    monkeypatch.setenv("OAOS_ENFORCE_SIGNED_CONTEXT", "1")
    c = TestClient(app)
    r = c.post("/v1/execute", json={"tool": "gmail_search", "action": "READ", "resource": "gmail/user/kim/*"}, headers={"X-Agent-Context": '{"tenant_id":"acme","user_id":"employee:kim"}'})
    assert r.status_code == 401
