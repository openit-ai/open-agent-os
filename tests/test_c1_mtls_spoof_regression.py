"""C1 regression — mTLS header spoof bypass (TDD, must fail before fix, pass after).

- Never trust client-controlled X-Client-Cert-CN / X-SSL-Client-CN / etc as mTLS proof
- Unknown CN must be rejected
- Tenant binding is mandatory (no mtls bypass)
- JWT path preserved
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

TEST_SIGNING_KEY = os.environ.get("OAOS_SECURITY_SERVICE_SIGNING_KEY") or os.environ.get("OAOS_SIGNING_KEY") or "test-unified-oaos-signing-key-32bytes-long-enough!!"
os.environ["OAOS_SIGNING_KEY"] = TEST_SIGNING_KEY
os.environ["OAOS_SECURITY_SERVICE_SIGNING_KEY"] = TEST_SIGNING_KEY
for _k in ("OAOS_USER_JWT_SIGNING_KEY","OAOS_JWT_SIGNING_KEY","OAOS_AGENT_CONTEXT_SIGNING_KEY","JWT_SIGNING_KEY","ADMIN_JWT_SECRET"):
    os.environ[_k] = TEST_SIGNING_KEY
os.environ.pop("OAOS_ENV", None)
# ensure mTLS disabled by default for this regression suite
os.environ.pop("OAOS_MTLS_ENABLED", None)
os.environ.pop("OAOS_MTLS_TRUSTED_PROXY", None)

from fastapi.testclient import TestClient
import importlib.util as _ilu
import sys as _sys

for _k in ("app", "auth", "infra", "business", "managed"):
    if _k in _sys.modules:
        _mod = _sys.modules[_k]
        _f = getattr(_mod, "__file__", "") or ""
        if "admin-console" in _f:
            del _sys.modules[_k]

_spec = _ilu.spec_from_file_location("security_app_c1_reg", str(ROOT / "security" / "app.py"))
_mod = _ilu.module_from_spec(_spec)
_sys.modules["security_app_c1_reg"] = _mod
_spec.loader.exec_module(_mod)
app = _mod.app

def _jwt(tenant_id="acme", iss="control-plane", aud="security", sub="agent:assistant:kim", exp_s=300, key=TEST_SIGNING_KEY, extra=None):
    now = datetime.now(timezone.utc)
    payload = {
        "iss": iss, "aud": aud, "sub": sub, "tenant_id": tenant_id,
        "session_id": "sess_test_123", "request_id": f"req_{uuid.uuid4().hex[:8]}",
        "exp": int((now + timedelta(seconds=exp_s)).timestamp()),
        "iat": int(now.timestamp()), "jti": uuid.uuid4().hex,
    }
    if extra:
        payload.update(extra)
        for k, v in list(payload.items()):
            if v is None and k in (extra or {}):
                del payload[k]
    return jwt.encode(payload, key, algorithm="HS256")

def _auth(tok): return {"Authorization": f"Bearer {tok}"}

SPOOF_HEADERS = [
    {"X-Client-Cert-CN": "control-plane"},
    {"X-SSL-Client-CN": "control-plane"},
    {"X-Client-CN": "control-plane"},
    {"X-MTLS-CN": "control-plane"},
    {"X-TLS-Client-CN": "control-plane"},
    {"X-Client-Cert-CN": "execution-gateway"},
    {"X-SSL-Client-CN": "execution-gateway"},
]

UNKNOWN_CN_HEADERS = [
    {"X-Client-Cert-CN": "evil-client"},
    {"X-SSL-Client-CN": "evil-client"},
    {"X-Client-CN": "attacker"},
    {"X-MTLS-CN": "unknown"},
    {"X-TLS-Client-CN": "hacker"},
]

PROTECTED = "/v1/policy/evaluate"
BODY_ACME = {"tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim", "resource": "x", "action": "READ"}

def test_spoofed_mtls_headers_rejected_without_jwt():
    """Every client-controlled mTLS header alone must NOT authenticate (401)."""
    c = TestClient(app)
    for h in SPOOF_HEADERS:
        r = c.post(PROTECTED, json=BODY_ACME, headers=h)
        assert r.status_code == 401, f"spoof header {h} should be 401, got {r.status_code} {r.text}"

def test_spoofed_headers_even_with_valid_tenant_body_still_rejected():
    c = TestClient(app)
    for h in SPOOF_HEADERS:
        r = c.post(PROTECTED, json=BODY_ACME, headers=h)
        assert r.status_code == 401

def test_unknown_cn_rejected():
    c = TestClient(app)
    for h in UNKNOWN_CN_HEADERS:
        r = c.post(PROTECTED, json=BODY_ACME, headers=h)
        assert r.status_code == 401, f"unknown CN {h} should be 401, got {r.status_code} {r.text}"

def test_spoofed_header_does_not_bypass_jwt_requirement_even_if_cn_allowlisted():
    """If attacker sends valid JWT for tenant A but spoofs header for tenant B, tenant binding must still enforce."""
    c = TestClient(app)
    token_acme = _jwt(tenant_id="acme")
    # try to use header to fake tenant binding bypass — must still 403 when body tenant mismatches JWT tenant
    for h in SPOOF_HEADERS:
        headers = {**_auth(token_acme), **h}
        r = c.post(PROTECTED, json={**BODY_ACME, "tenant_id": "evil-tenant"}, headers=headers)
        assert r.status_code in (401, 403), f"tenant mismatch with spoof header {h} should be 403/401, got {r.status_code} {r.text}"

def test_tenant_binding_mandatory_with_valid_jwt():
    c = TestClient(app)
    token_acme = _jwt(tenant_id="acme")
    r = c.post(PROTECTED, json={**BODY_ACME, "tenant_id": "evil-tenant"}, headers=_auth(token_acme))
    assert r.status_code in (401, 403), f"expected tenant mismatch 403, got {r.status_code} {r.text}"

def test_tenant_binding_allows_matching_tenant():
    c = TestClient(app)
    token_acme = _jwt(tenant_id="acme")
    r = c.post(PROTECTED, json=BODY_ACME, headers=_auth(token_acme))
    assert r.status_code == 200, r.text

def test_jwt_path_preserved_valid_cp_jwt_accepted():
    c = TestClient(app)
    tok = _jwt()
    r = c.post(PROTECTED, json=BODY_ACME, headers=_auth(tok))
    assert r.status_code == 200, r.text

def test_jwt_path_preserved_invalid_signature_rejected():
    c = TestClient(app)
    tok = _jwt(key="wrong-key-32bytes-long-enough-not-match")
    r = c.post(PROTECTED, json=BODY_ACME, headers=_auth(tok))
    assert r.status_code == 401

def test_mtls_header_bypass_disabled_in_production_fail_closed():
    os.environ["OAOS_ENV"] = "production"
    try:
        c = TestClient(app)
        for h in SPOOF_HEADERS:
            r = c.post(PROTECTED, json=BODY_ACME, headers=h)
            assert r.status_code == 401, f"prod spoof {h} must be 401, got {r.status_code}"
    finally:
        os.environ.pop("OAOS_ENV", None)
