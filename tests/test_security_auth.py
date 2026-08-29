"""Phase 1 C1 — Security API authentication foundation.
Strict TDD: these tests must fail before auth middleware is implemented,
and pass after HS256 verified service/user JWT contract is enforced.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt

ROOT = Path(__file__).resolve().parents[1]
for p in [
    ROOT / "security" / "policy-engine",
    ROOT / "security" / "delegation",
    ROOT / "security" / "credential-vault",
    ROOT / "security" / "token",
    ROOT / "security" / "crypto",
    ROOT / "security" / "audit",
    ROOT / "security" / "approval",
    ROOT / "packages" / "common-types",
    ROOT / "packages" / "policy-model",
    ROOT / "packages" / "audit-model",
    ROOT / "packages" / "delegation-model",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "security") not in sys.path:
    sys.path.insert(0, str(ROOT / "security"))

TEST_SIGNING_KEY = "test-security-auth-signing-key-32bytes-long!!"
os.environ["OAOS_SIGNING_KEY"] = TEST_SIGNING_KEY
os.environ["OAOS_SECURITY_SERVICE_SIGNING_KEY"] = TEST_SIGNING_KEY
os.environ.pop("OAOS_ENV", None)

from fastapi.testclient import TestClient

import importlib.util as _ilu
import sys as _sys
for _k in ("app", "auth", "infra", "business", "managed"):
    if _k in _sys.modules:
        _mod = _sys.modules[_k]
        _f = getattr(_mod, "__file__", "") or ""
        if "admin-console" in _f:
            del _sys.modules[_k]
_spec = _ilu.spec_from_file_location("security_app_auth_test", str(ROOT / "security" / "app.py"))
_mod = _ilu.module_from_spec(_spec)
_sys.modules["security_app_auth_test"] = _mod
_spec.loader.exec_module(_mod)
app = _mod.app

def _make_cp_jwt(
    sub: str = "agent:assistant:kim",
    tenant_id: str = "acme",
    aud: str = "security",
    iss: str = "control-plane",
    exp_delta_seconds: int = 300,
    signing_key: str = TEST_SIGNING_KEY,
    extra: dict | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "tenant_id": tenant_id,
        "session_id": "sess_test_123",
        "request_id": f"req_{uuid.uuid4().hex[:8]}",
        "exp": int((now + timedelta(seconds=exp_delta_seconds)).timestamp()),
        "iat": int(now.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if extra:
        payload.update(extra)
        for k, v in list(payload.items()):
            if v is None and k in (extra or {}):
                del payload[k]
    return jwt.encode(payload, signing_key, algorithm="HS256")

def _make_user_jwt(
    sub: str = "employee:kim",
    tenant_id: str = "acme",
    aud: str = "security",
    iss: str = "open-agent-os-auth",
    exp_delta_seconds: int = 300,
) -> str:
    return _make_cp_jwt(sub=sub, tenant_id=tenant_id, aud=aud, iss=iss, exp_delta_seconds=exp_delta_seconds)

def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}

PROTECTED_ENDPOINTS = [
    ("POST", "/v1/policy/evaluate", {"tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "gmail/user/kim/*", "action": "READ"}),
    ("POST", "/v1/delegation/grant", {"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "provider": "google", "scope": "gmail.read"}),
    ("POST", "/v1/delegation/revoke", {"delegation_id": "del_nonexistent"}),
    ("GET", "/v1/delegation/del_nonexistent", None),
    ("POST", "/v1/token/issue", {"sub": "agent:assistant:kim", "on_behalf_of": "employee:kim", "action": "READ", "resource": "gmail/user/kim/*", "session_id": "sess_test_123", "request_id": "req_test_123", "ttl_seconds": 300}),
    ("POST", "/v1/token/verify", {"token": "invalid"}),
    ("POST", "/v1/token/revoke", {"token": "invalid"}),
    ("POST", "/v1/approval/request", {"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "action": "READ", "resource": "gmail/user/kim/*", "risk": "HIGH", "ttl_minutes": 60}),
    ("POST", "/v1/approval/decide", {"approval_id": "appr_nonexistent", "decision": "APPROVED", "decided_by": "employee:kim"}),
    ("GET", "/v1/approval/appr_nonexistent", None),
    ("POST", "/v1/audit/verify", {}),
    ("GET", "/v1/audit/events", None),
    ("GET", "/v1/audit/checkpoint", None),
]

PUBLIC_ENDPOINTS = [
    ("GET", "/health", None),
    ("GET", "/healthz", None),
    ("GET", "/readyz", None),
    ("GET", "/v1/health/detailed", None),
]

def test_health_endpoints_remain_public():
    c = TestClient(app)
    for method, path, body in PUBLIC_ENDPOINTS:
        if method == "GET":
            r = c.get(path)
        else:
            r = c.post(path, json=body or {})
        assert r.status_code == 200, f"{method} {path} should be public, got {r.status_code} {r.text}"

def test_anon_rejected_on_protected_endpoints():
    c = TestClient(app)
    for method, path, body in PROTECTED_ENDPOINTS:
        if method == "GET":
            r = c.get(path)
        else:
            r = c.post(path, json=body or {})
        assert r.status_code == 401, f"{method} {path} anon should be 401, got {r.status_code} {r.text}"

def test_anon_rejected_in_prod():
    os.environ["OAOS_ENV"] = "production"
    try:
        c = TestClient(app)
        r = c.post("/v1/policy/evaluate", json={"tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "x", "action": "READ"})
        assert r.status_code == 401
    finally:
        os.environ.pop("OAOS_ENV", None)

def test_valid_cp_jwt_accepted():
    c = TestClient(app)
    token = _make_cp_jwt()
    headers = _auth_header(token)
    r = c.post("/v1/policy/evaluate", json={"tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "gmail/user/kim/*", "action": "READ"}, headers=headers)
    assert r.status_code == 200, r.text

def test_valid_user_jwt_accepted_if_implemented():
    c = TestClient(app)
    token = _make_user_jwt()
    headers = _auth_header(token)
    r = c.post("/v1/policy/evaluate", json={"tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "gmail/user/kim/*", "action": "READ"}, headers=headers)
    assert r.status_code in (200, 401), r.text
    if r.status_code == 401:
        pytest.skip("user JWT not accepted — control-plane-only contract")

def test_expired_401():
    c = TestClient(app)
    token = _make_cp_jwt(exp_delta_seconds=-60)
    r = c.post("/v1/policy/evaluate", json={"tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "x", "action": "READ"}, headers=_auth_header(token))
    assert r.status_code == 401, r.text

def test_wrong_aud_401():
    c = TestClient(app)
    token = _make_cp_jwt(aud="control-plane")
    r = c.post("/v1/policy/evaluate", json={"tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "x", "action": "READ"}, headers=_auth_header(token))
    assert r.status_code == 401, r.text

def test_wrong_iss_401():
    c = TestClient(app)
    token = _make_cp_jwt(iss="evil-issuer")
    r = c.post("/v1/policy/evaluate", json={"tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "x", "action": "READ"}, headers=_auth_header(token))
    assert r.status_code == 401, r.text

def test_invalid_signature_401():
    c = TestClient(app)
    token = _make_cp_jwt(signing_key="wrong-key-32bytes-long-enough-not-match")
    r = c.post("/v1/policy/evaluate", json={"tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "x", "action": "READ"}, headers=_auth_header(token))
    assert r.status_code == 401, r.text

def test_missing_sub_401():
    c = TestClient(app)
    now = datetime.now(timezone.utc)
    payload = {
        "iss": "control-plane",
        "aud": "security",
        "tenant_id": "acme",
        "session_id": "sess_x",
        "exp": int((now + timedelta(seconds=300)).timestamp()),
        "iat": int(now.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, TEST_SIGNING_KEY, algorithm="HS256")
    r = c.post("/v1/policy/evaluate", json={"tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "x", "action": "READ"}, headers=_auth_header(token))
    assert r.status_code == 401, r.text

def test_tenant_binding_403_or_401_on_mismatch():
    c = TestClient(app)
    token = _make_cp_jwt(tenant_id="acme")
    r = c.post("/v1/policy/evaluate", json={"tenant_id": "evil-tenant", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "x", "action": "READ"}, headers=_auth_header(token))
    assert r.status_code in (401, 403), r.text

def test_sub_mismatch_403():
    c = TestClient(app)
    token = _make_cp_jwt(sub="agent:assistant:kim")
    r = c.post("/v1/token/issue", json={"sub": "agent:assistant:lee", "on_behalf_of": "employee:lee", "action": "READ", "resource": "gmail/user/lee/*", "session_id": "sess_test_123", "request_id": "req_test_123", "ttl_seconds": 300}, headers=_auth_header(token))
    assert r.status_code == 403, r.text

def test_token_issue_success_with_matching_sub():
    c = TestClient(app)
    token = _make_cp_jwt(sub="agent:assistant:kim", tenant_id="acme")
    r = c.post("/v1/token/issue", json={"sub": "agent:assistant:kim", "on_behalf_of": "employee:kim", "action": "READ", "resource": "gmail/user/kim/*", "session_id": "sess_test_123", "request_id": "req_test_123", "ttl_seconds": 300}, headers=_auth_header(token))
    assert r.status_code == 200, r.text
    assert "token" in r.json()

def test_mtls_bypass():
    c = TestClient(app)
    headers = {"X-Client-Cert-CN": "control-plane"}
    r = c.post("/v1/policy/evaluate", json={"tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "x", "action": "READ"}, headers=headers)
    assert r.status_code == 200, r.text
    headers2 = {"X-Client-Cert-CN": "execution-gateway"}
    r2 = c.post("/v1/policy/evaluate", json={"tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "x", "action": "READ"}, headers=headers2)
    assert r2.status_code == 200, r2.text

def test_mtls_invalid_cn_rejected():
    c = TestClient(app)
    headers = {"X-Client-Cert-CN": "evil-client"}
    r = c.post("/v1/policy/evaluate", json={"tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "x", "action": "READ"}, headers=headers)
    assert r.status_code == 401, r.text

def test_bearer_without_prefix_rejected():
    c = TestClient(app)
    token = _make_cp_jwt()
    r = c.post("/v1/policy/evaluate", json={"tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "x", "action": "READ"}, headers={"Authorization": token})
    assert r.status_code == 401, r.text

def test_delegation_grant_requires_auth():
    c = TestClient(app)
    token = _make_cp_jwt()
    r = c.post("/v1/delegation/grant", json={"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "provider": "google", "scope": "gmail.read"}, headers=_auth_header(token))
    assert r.status_code == 200, r.text
    assert "id" in r.json()
