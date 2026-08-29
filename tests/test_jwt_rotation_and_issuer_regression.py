"""Regression: runtime env/key rotation and issuer/audience mismatch — C1-H3 dynamic resolution.

- No module-level stale snapshots: changing env after import must be picked up (dynamic getter).
- Issuer/audience mismatch must be 401 fail-closed.
- Production fail-closed when dev key used is preserved.
- Covers security/auth (C1), control-plane auth (H1), execution-gateway signed_context (H2), personal-wiki auth (H3), admin-console auth (H3-Admin).
"""
from __future__ import annotations
import os
import sys
import importlib
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from jose import jwt

ROOT = Path(__file__).resolve().parents[1]
UNIFIED = "test-unified-oaos-signing-key-32bytes-long-enough!!"
KEY_A = "rotation-key-A-32bytes-long-enough-111111"
KEY_B = "rotation-key-B-32bytes-long-enough-222222"

# Ensure fresh imports see UNIFIED first
for _k in ("OAOS_SIGNING_KEY","OAOS_SECURITY_SERVICE_SIGNING_KEY","OAOS_USER_JWT_SIGNING_KEY","OAOS_JWT_SIGNING_KEY","OAOS_AGENT_CONTEXT_SIGNING_KEY","JWT_SIGNING_KEY","ADMIN_JWT_SECRET"):
    os.environ[_k] = UNIFIED
os.environ.pop("OAOS_ENV", None)
# issuer/audience defaults for CP/H1 and EGW/H2 so tests don't depend on conftest setdefault
os.environ.setdefault("OAOS_USER_JWT_ISSUER","open-agent-os-auth")
os.environ.setdefault("OAOS_JWT_ISSUER","open-agent-os-auth")
os.environ.setdefault("OAOS_USER_JWT_AUDIENCE","control-plane")
os.environ.setdefault("OAOS_JWT_AUDIENCE","control-plane")
os.environ.setdefault("OAOS_AGENT_CONTEXT_ISSUER","control-plane")
os.environ.setdefault("OAOS_AGENT_CONTEXT_AUDIENCE","execution-gateway")

def _make_jwt(payload: dict, key: str) -> str:
    return jwt.encode(payload, key, algorithm="HS256")

def _cp_payload(iss="open-agent-os-auth", aud="control-plane", key=UNIFIED, tenant="acme", sub="employee:kim"):
    now = datetime.now(timezone.utc)
    return {
        "iss": iss, "aud": aud, "sub": sub, "tenant_id": tenant,
        "exp": int((now+timedelta(seconds=300)).timestamp()),
        "iat": int(now.timestamp()), "jti": uuid.uuid4().hex,
    }

def _egw_payload(iss="control-plane", aud="execution-gateway", key=UNIFIED):
    now = datetime.now(timezone.utc)
    return {
        "iss": iss, "aud": aud,
        "tenant_id": "acme", "user_id": "employee:kim", "agent_id": "agent:assistant:kim",
        "session_id": "sess_123", "trace_id": "trace_123", "request_id": "req_123",
        "exp": int((now+timedelta(seconds=300)).timestamp()),
        "iat": int(now.timestamp()), "jti": uuid.uuid4().hex,
    }

def _wiki_payload(iss="control-plane", aud="wiki-fs", key=UNIFIED):
    now = datetime.now(timezone.utc)
    return {
        "iss": iss, "aud": aud, "sub": "employee:kim",
        "tenant_id": "acme", "agent_id": "agent:assistant:kim", "scope": "wiki:read",
        "exp": int((now+timedelta(seconds=300)).timestamp()),
        "iat": int(now.timestamp()), "jti": uuid.uuid4().hex,
    }

def _security_payload(iss="control-plane", aud="security", key=UNIFIED):
    now = datetime.now(timezone.utc)
    return {
        "iss": iss, "aud": aud, "sub": "agent:assistant:kim", "tenant_id": "acme",
        "session_id": "sess_123",
        "exp": int((now+timedelta(seconds=300)).timestamp()),
        "iat": int(now.timestamp()), "jti": uuid.uuid4().hex,
    }

# --- Helpers to import fresh modules with file-location loader to avoid auth collision ---

def _load_security_auth():
    import importlib.util
    p = ROOT / "security" / "auth.py"
    spec = importlib.util.spec_from_file_location("_sec_auth_rot", str(p))
    m = importlib.util.module_from_spec(spec)  # type: ignore
    spec.loader.exec_module(m)  # type: ignore
    return m

def _load_cp_auth():
    import importlib.util
    p = ROOT / "control-plane" / "control_plane" / "auth.py"
    spec = importlib.util.spec_from_file_location("_cp_auth_rot", str(p))
    m = importlib.util.module_from_spec(spec)  # type: ignore
    spec.loader.exec_module(m)  # type: ignore
    return m

def _load_cp_signed():
    import importlib.util
    p = ROOT / "control-plane" / "control_plane" / "signed_context.py"
    spec = importlib.util.spec_from_file_location("_cp_signed_rot", str(p))
    m = importlib.util.module_from_spec(spec)  # type: ignore
    spec.loader.exec_module(m)  # type: ignore
    return m

def _load_egw_signed():
    import importlib.util
    p = ROOT / "execution-gateway" / "execution_gateway" / "signed_context.py"
    spec = importlib.util.spec_from_file_location("_egw_signed_rot", str(p))
    m = importlib.util.module_from_spec(spec)  # type: ignore
    spec.loader.exec_module(m)  # type: ignore
    return m

def _load_wiki_auth():
    import importlib.util
    p = ROOT / "packages" / "personal-wiki" / "personal_wiki" / "auth.py"
    spec = importlib.util.spec_from_file_location("_wiki_auth_rot", str(p))
    m = importlib.util.module_from_spec(spec)  # type: ignore
    spec.loader.exec_module(m)  # type: ignore
    return m

def _load_admin_auth():
    import importlib.util
    p = ROOT / "admin-console" / "backend" / "auth.py"
    spec = importlib.util.spec_from_file_location("_admin_auth_rot", str(p))
    m = importlib.util.module_from_spec(spec)  # type: ignore
    spec.loader.exec_module(m)  # type: ignore
    return m


# ---- Tests ----

def test_security_auth_env_rotation_after_import():
    m = _load_security_auth()
    # start with KEY_A
    os.environ["OAOS_SECURITY_SERVICE_SIGNING_KEY"] = KEY_A
    os.environ["OAOS_SIGNING_KEY"] = KEY_A
    tok_a = _make_jwt(_security_payload(key=KEY_A), KEY_A)
    # verify with KEY_A succeeds
    payload = m._verify_jwt(tok_a)
    assert payload["tenant_id"] == "acme"
    # rotate to KEY_B — old token must fail, new token must pass (no stale snapshot)
    os.environ["OAOS_SECURITY_SERVICE_SIGNING_KEY"] = KEY_B
    os.environ["OAOS_SIGNING_KEY"] = KEY_B
    with pytest.raises(HTTPException) as ei:
        m._verify_jwt(tok_a)
    assert ei.value.status_code == 401
    tok_b = _make_jwt(_security_payload(key=KEY_B), KEY_B)
    payload2 = m._verify_jwt(tok_b)
    assert payload2["tenant_id"] == "acme"
    # issuer/audience mismatch must be 401, not 200
    bad_iss = _make_jwt(_security_payload(iss="evil-issuer", key=KEY_B), KEY_B)
    with pytest.raises(HTTPException) as ei2:
        m._verify_jwt(bad_iss)
    assert ei2.value.status_code == 401
    assert "issuer" in str(ei2.value.detail).lower()
    bad_aud = _make_jwt(_security_payload(aud="evil-aud", key=KEY_B), KEY_B)
    with pytest.raises(HTTPException) as ei3:
        m._verify_jwt(bad_aud)
    assert ei3.value.status_code == 401
    assert "audience" in str(ei3.value.detail).lower()
    # restore
    os.environ["OAOS_SECURITY_SERVICE_SIGNING_KEY"] = UNIFIED
    os.environ["OAOS_SIGNING_KEY"] = UNIFIED

def test_cp_auth_env_rotation_and_issuer_audience_mismatch():
    m = _load_cp_auth()
    os.environ["OAOS_USER_JWT_SIGNING_KEY"] = KEY_A
    for k in ("OAOS_JWT_SIGNING_KEY","OAOS_SIGNING_KEY","ADMIN_JWT_SECRET"):
        os.environ.pop(k, None)
    # clean issuer/audience env to use defaults
    for k in ("OAOS_USER_JWT_ISSUER","OAOS_JWT_ISSUER","OAOS_AUTH_ISSUER","OAOS_USER_JWT_AUDIENCE","OAOS_JWT_AUDIENCE","OAOS_AUTH_AUDIENCE"):
        os.environ.pop(k, None)
    os.environ["OAOS_USER_JWT_ISSUER"] = "open-agent-os-auth"
    os.environ["OAOS_USER_JWT_AUDIENCE"] = "control-plane"
    tok_a = _make_jwt(_cp_payload(key=KEY_A, iss="open-agent-os-auth", aud="control-plane"), KEY_A)
    assert m.verify_user_jwt(tok_a)["tenant_id"] == "acme"
    # Check dynamic issuer via __getattr__: change issuer env after import
    os.environ["OAOS_USER_JWT_ISSUER"] = "rotated-issuer"
    tok_new_iss = _make_jwt(_cp_payload(key=KEY_A, iss="rotated-issuer", aud="control-plane"), KEY_A)
    # should accept rotated issuer, reject old
    assert m.verify_user_jwt(tok_new_iss)["sub"] == "employee:kim"
    with pytest.raises(HTTPException) as ei:
        m.verify_user_jwt(tok_a)
    assert ei.value.status_code == 401
    # rotate key
    os.environ["OAOS_USER_JWT_SIGNING_KEY"] = KEY_B
    with pytest.raises(HTTPException) as ei2:
        m.verify_user_jwt(tok_new_iss)
    assert ei2.value.status_code == 401
    tok_b = _make_jwt(_cp_payload(key=KEY_B, iss="rotated-issuer", aud="control-plane"), KEY_B)
    assert m.verify_user_jwt(tok_b)["tenant_id"] == "acme"
    # audience mismatch
    os.environ["OAOS_USER_JWT_AUDIENCE"] = "control-plane"
    # keep issuer rotated-issuer for this check, but audience wrong
    bad_aud = _make_jwt(_cp_payload(key=KEY_B, iss="rotated-issuer", aud="evil-aud"), KEY_B)
    with pytest.raises(HTTPException) as ei3:
        m.verify_user_jwt(bad_aud)
    assert ei3.value.status_code == 401
    # ensure module-level __getattr__ reflects rotation (no stale snapshot)
    assert m.EXPECTED_ISSUER == "rotated-issuer"
    assert m.EXPECTED_AUDIENCE == "control-plane"
    # cross-check: changing to another issuer is picked up
    os.environ["OAOS_USER_JWT_ISSUER"] = "open-agent-os-auth"
    assert m.EXPECTED_ISSUER == "open-agent-os-auth"
    # restore
    os.environ["OAOS_USER_JWT_SIGNING_KEY"] = UNIFIED
    os.environ["OAOS_USER_JWT_ISSUER"] = "open-agent-os-auth"
    os.environ["OAOS_USER_JWT_AUDIENCE"] = "control-plane"

def test_egw_signed_context_rotation_and_issuer_audience():
    m = _load_egw_signed()
    os.environ["OAOS_AGENT_CONTEXT_SIGNING_KEY"] = KEY_A
    os.environ.pop("OAOS_SIGNING_KEY", None)
    for k in ("OAOS_AGENT_CONTEXT_ISSUER","OAOS_SIGNED_CONTEXT_ISSUER","OAOS_AGENT_CONTEXT_AUDIENCE","OAOS_SIGNED_CONTEXT_AUDIENCE"):
        os.environ.pop(k, None)
    tok_a = _make_jwt(_egw_payload(iss="control-plane", aud="execution-gateway"), KEY_A)
    assert m.verify_agent_context_jwt(tok_a)["tenant_id"] == "acme"
    # dynamic issuer/audience via __getattr__
    assert m.ISSUER == "control-plane"
    assert m.AUDIENCE == "execution-gateway"
    os.environ["OAOS_AGENT_CONTEXT_ISSUER"] = "evil-issuer"
    # old token with old iss should now fail because expected issuer changed
    with pytest.raises(HTTPException) as ei:
        m.verify_agent_context_jwt(tok_a)
    assert ei.value.status_code == 401
    tok_new = _make_jwt(_egw_payload(iss="evil-issuer", aud="execution-gateway"), KEY_A)
    assert m.verify_agent_context_jwt(tok_new)["tenant_id"] == "acme"
    # rotate key
    os.environ["OAOS_AGENT_CONTEXT_ISSUER"] = "control-plane"
    os.environ["OAOS_AGENT_CONTEXT_SIGNING_KEY"] = KEY_B
    with pytest.raises(HTTPException) as ei2:
        m.verify_agent_context_jwt(tok_a)
    assert ei2.value.status_code == 401
    tok_b = _make_jwt(_egw_payload(iss="control-plane", aud="execution-gateway"), KEY_B)
    assert m.verify_agent_context_jwt(tok_b)["tenant_id"] == "acme"
    # audience mismatch
    bad_aud = _make_jwt(_egw_payload(iss="control-plane", aud="bad-aud"), KEY_B)
    with pytest.raises(HTTPException) as ei3:
        m.verify_agent_context_jwt(bad_aud)
    assert ei3.value.status_code == 401
    # restore
    os.environ["OAOS_AGENT_CONTEXT_SIGNING_KEY"] = UNIFIED
    for k in ("OAOS_AGENT_CONTEXT_ISSUER","OAOS_SIGNED_CONTEXT_ISSUER","OAOS_AGENT_CONTEXT_AUDIENCE","OAOS_SIGNED_CONTEXT_AUDIENCE"):
        os.environ.pop(k, None)

def test_cp_signed_context_rotation():
    m = _load_cp_signed()
    os.environ["OAOS_AGENT_CONTEXT_SIGNING_KEY"] = KEY_A
    for k in ("OAOS_SIGNING_KEY","OAOS_JWT_SIGNING_KEY"):
        os.environ.pop(k, None)
    for k in ("OAOS_AGENT_CONTEXT_ISSUER","OAOS_SIGNED_CONTEXT_ISSUER","OAOS_AGENT_CONTEXT_AUDIENCE","OAOS_SIGNED_CONTEXT_AUDIENCE"):
        os.environ.pop(k, None)
    # issue with KEY_A should be verifiable only after ensuring EGW verifier uses same key; here just test issuer/audience dynamic getter
    assert m.get_issuer() == "control-plane"
    assert m.get_audience() == "execution-gateway"
    assert m.ISSUER == "control-plane"
    assert m.AUDIENCE == "execution-gateway"
    os.environ["OAOS_AGENT_CONTEXT_ISSUER"] = "rotated-issuer"
    assert m.ISSUER == "rotated-issuer"
    assert m.get_issuer() == "rotated-issuer"
    # rotation key: issue with new key vs old
    tok_a = m.issue_agent_context_jwt(tenant_id="acme", user_id="employee:kim", agent_id="agent:assistant:kim", session_id="sess_123", signing_key=KEY_A, issuer="rotated-issuer")
    # manually verify token was signed with KEY_A and rotated issuer
    claims = jwt.get_unverified_claims(tok_a)
    assert claims["iss"] == "rotated-issuer"
    # now rotate key env
    os.environ["OAOS_AGENT_CONTEXT_SIGNING_KEY"] = KEY_B
    tok_b = m.issue_agent_context_jwt(tenant_id="acme", user_id="employee:kim", agent_id="agent:assistant:kim", session_id="sess_123", signing_key=None)
    # tok_b should be signed with KEY_B and current issuer
    claims_b = jwt.decode(tok_b, KEY_B, algorithms=["HS256"], options={"verify_aud": False, "verify_iss": False})
    assert claims_b["iss"] == "rotated-issuer"
    # old token decode with new key should fail
    with pytest.raises(Exception):
        jwt.decode(tok_a, KEY_B, algorithms=["HS256"], audience="execution-gateway", issuer="rotated-issuer")
    # restore
    os.environ["OAOS_AGENT_CONTEXT_SIGNING_KEY"] = UNIFIED
    for k in ("OAOS_AGENT_CONTEXT_ISSUER","OAOS_SIGNED_CONTEXT_ISSUER"):
        os.environ.pop(k, None)

def test_wiki_auth_rotation_and_issuer_audience():
    m = _load_wiki_auth()
    os.environ["OAOS_SIGNING_KEY"] = KEY_A
    for k in ("OAOS_SECURITY_SERVICE_SIGNING_KEY","JWT_SIGNING_KEY","ADMIN_JWT_SECRET","OAOS_JWT_SIGNING_KEY"):
        os.environ.pop(k, None)
    tok_a = _make_jwt(_wiki_payload(iss="control-plane", aud="wiki-fs"), KEY_A)
    assert m.verify_wiki_jwt(tok_a, required_scope="wiki:read")["tenant_id"] == "acme"
    os.environ["OAOS_SIGNING_KEY"] = KEY_B
    with pytest.raises(HTTPException) as ei:
        m.verify_wiki_jwt(tok_a, required_scope="wiki:read")
    assert ei.value.status_code == 401
    tok_b = _make_jwt(_wiki_payload(iss="control-plane", aud="wiki-fs"), KEY_B)
    assert m.verify_wiki_jwt(tok_b, required_scope="wiki:read")["tenant_id"] == "acme"
    # issuer mismatch
    bad_iss = _make_jwt(_wiki_payload(iss="evil-issuer", aud="wiki-fs"), KEY_B)
    with pytest.raises(HTTPException) as ei2:
        m.verify_wiki_jwt(bad_iss, required_scope="wiki:read")
    assert ei2.value.status_code == 401
    # audience mismatch
    bad_aud = _make_jwt(_wiki_payload(iss="control-plane", aud="evil-aud"), KEY_B)
    with pytest.raises(HTTPException) as ei3:
        m.verify_wiki_jwt(bad_aud, required_scope="wiki:read")
    assert ei3.value.status_code == 401
    os.environ["OAOS_SIGNING_KEY"] = UNIFIED

def test_admin_auth_rotation_and_fail_closed():
    # Use file-location loader consistent with test_admin_backend — avoids pydantic double-definition;
    # we don't need isolated reload; dynamic getter already ensures rotation without reload.
    import importlib.util
    p = ROOT / "admin-console" / "backend" / "auth.py"
    # Try to reuse already-loaded canonical module if present, else file-load a unique instance
    mod_name = "admin_auth_rotation_check"
    # Ensure backend dir on path for loader's relative imports
    if str(p.parent) not in sys.path:
        sys.path.insert(0, str(p.parent))
    spec = importlib.util.spec_from_file_location(mod_name, str(p))
    m = importlib.util.module_from_spec(spec)  # type: ignore
    _prev_auth = sys.modules.get("auth")
    _prev_mod = sys.modules.get(mod_name)
    sys.modules[mod_name] = m
    sys.modules["auth"] = m  # backend/auth.py does `from auth import` fallback; provide bare alias
    try:
        spec.loader.exec_module(m)  # type: ignore
        # Pydantic v2 needs model_rebuild when same file is loaded twice in one process
        try:
            m.AdminUser.model_rebuild()  # type: ignore
            m.AdminUserPublic.model_rebuild()  # type: ignore
        except Exception:
            pass
        os.environ["ADMIN_JWT_SECRET"] = KEY_A
        os.environ.pop("OAOS_ENV", None)
        tok_a, _ = m._create_jwt("admin@openit.co.kr", "L5")
        # verify via get_current_admin path (dynamic getter)
        # decode manually to ensure it was signed with KEY_A
        payload_a = jwt.decode(tok_a, KEY_A, algorithms=["HS256"])
        assert payload_a["sub"] == "admin@openit.co.kr"
        # rotate
        os.environ["ADMIN_JWT_SECRET"] = KEY_B
        # old token must fail with new secret
        with pytest.raises(Exception):
            jwt.decode(tok_a, KEY_B, algorithms=["HS256"])
        # new token with new secret must succeed
        tok_b, _ = m._create_jwt("admin@openit.co.kr", "L5")
        payload_b = jwt.decode(tok_b, KEY_B, algorithms=["HS256"])
        assert payload_b["sub"] == "admin@openit.co.kr"
        # __getattr__ dynamic
        assert m.JWT_SECRET == KEY_B
        os.environ["ADMIN_JWT_SECRET"] = KEY_A
        assert m.JWT_SECRET == KEY_A
        # production fail-closed: setting OAOS_ENV=production with dev key must raise on get_jwt_secret
        os.environ["OAOS_ENV"] = "production"
        os.environ["ADMIN_JWT_SECRET"] = "dev-admin-jwt-secret-please-change"
        with pytest.raises(RuntimeError):
            m.get_jwt_secret()
    finally:
        # restore previous modules to avoid bare-alias pollution for later tests (L4 fixture regression)
        if _prev_mod is None:
            sys.modules.pop(mod_name, None)
        else:
            sys.modules[mod_name] = _prev_mod
        if _prev_auth is None:
            sys.modules.pop("auth", None)
        else:
            sys.modules["auth"] = _prev_auth
        os.environ.pop("OAOS_ENV", None)
        os.environ["ADMIN_JWT_SECRET"] = UNIFIED

def test_security_app_signing_key_rotation_via_middleware():
    import importlib.util
    p = ROOT / "security" / "app.py"
    # set initial key
    os.environ["OAOS_SECURITY_SERVICE_SIGNING_KEY"] = KEY_A
    os.environ["OAOS_SIGNING_KEY"] = KEY_A
    os.environ.pop("OAOS_ENV", None)
    spec = importlib.util.spec_from_file_location("_sec_app_rot", str(p))
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    spec.loader.exec_module(mod)  # type: ignore
    # initial SIGNING_KEY via __getattr__
    assert mod.get_signing_key() == KEY_A
    assert mod.SIGNING_KEY == KEY_A
    # rotate env
    os.environ["OAOS_SECURITY_SERVICE_SIGNING_KEY"] = KEY_B
    assert mod.get_signing_key() == KEY_B
    assert mod.SIGNING_KEY == KEY_B
    # ensure middleware helper refreshes singletons
    # simulate _ensure_signing_key_fresh
    old_token_key = mod.token_service.signing_key
    # old should be KEY_A before refresh
    assert old_token_key == KEY_A
    mod._ensure_signing_key_fresh()
    assert mod.token_service.signing_key == KEY_B
    assert mod.approval_store.signing_key == KEY_B
    # restore
    os.environ["OAOS_SECURITY_SERVICE_SIGNING_KEY"] = UNIFIED
    os.environ["OAOS_SIGNING_KEY"] = UNIFIED
    mod._ensure_signing_key_fresh()
    assert mod.token_service.signing_key == UNIFIED
