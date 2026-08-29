"""H3 — Personal Wiki JWT / Ownership verification (TDD, fail-closed).

Verifies:
- Production owner resolution uses verified JWT only (no get_unverified_claims, no X-User-Id fallback)
- JWT must have valid issuer/audience/exp/tenant_id/agent_id/scope
- Cross-tenant and cross-agent are rejected 401/403
- Path traversal is rejected 403
- Body tenant_id is ignored (JWT tenant is authoritative)
- Expired / wrong iss / wrong aud / missing scope -> 401
- Explicit non-prod test fixture only: X-User-Id allowed only when OAOS_ENV != production and PYTEST_CURRENT_TEST set

Vault FS unit tests are in same file for convenience.
"""
from __future__ import annotations

import os
import sys
import uuid
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"
PKG_WIKI = ROOT / "packages" / "personal-wiki"

# Ensure package wiki is importable (qualified imports only; do not pollute bare 'auth')
if str(PKG_WIKI) not in sys.path:
    sys.path.insert(0, str(PKG_WIKI))
# NOTE: BACKEND is not added to sys.path to avoid bare 'auth' collision (admin vs security).
# Admin app is loaded via package-qualified spec (admin_console.backend.app).

TEST_SIGNING_KEY = os.environ.get("OAOS_SIGNING_KEY") or os.environ.get("OAOS_SECURITY_SERVICE_SIGNING_KEY") or "test-unified-oaos-signing-key-32bytes-long-enough!!"
os.environ["OAOS_SIGNING_KEY"] = TEST_SIGNING_KEY
os.environ["OAOS_SECURITY_SERVICE_SIGNING_KEY"] = TEST_SIGNING_KEY
# Ensure all verifiers (CP/EGW/wiki/admin) see same unified key
for _k in ("OAOS_USER_JWT_SIGNING_KEY","OAOS_JWT_SIGNING_KEY","OAOS_AGENT_CONTEXT_SIGNING_KEY","JWT_SIGNING_KEY","ADMIN_JWT_SECRET"):
    os.environ[_k] = TEST_SIGNING_KEY
os.environ["JWT_SIGNING_KEY"] = TEST_SIGNING_KEY

# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def _make_wiki_jwt(
    sub: str = "employee:kim",
    tenant_id: str = "acme",
    agent_id: str = "agent:assistant:kim",
    aud: str = "wiki-fs",
    iss: str = "control-plane",
    scope: str = "wiki:read",
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
        "agent_id": agent_id,
        "scope": scope,
        "exp": int((now + timedelta(seconds=exp_delta_seconds)).timestamp()),
        "iat": int(now.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if extra:
        for k, v in extra.items():
            if v is None:
                payload.pop(k, None)
            else:
                payload[k] = v
        # handle deletion of keys explicitly set to None in extra
        for k in list(extra.keys()):
            if extra[k] is None and k in payload:
                del payload[k]
    return jwt.encode(payload, signing_key, algorithm="HS256")

def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}

# ---------------------------------------------------------------------------
# Lazy load admin app (personal_wiki router)
# ---------------------------------------------------------------------------

def _load_admin_app():
    import importlib.util, importlib.machinery, types
    # Ensure admin_console package exists (mirrors admin app's _ensure_admin_package)
    for pkg in ("admin_console", "admin_console.backend"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []  # type: ignore
            m.__spec__ = importlib.machinery.ModuleSpec(pkg, None, is_package=True)  # type: ignore
            sys.modules[pkg] = m
    try:
        import admin_console.backend.app as app_mod  # type: ignore
        return app_mod.app
    except Exception:
        import importlib.util as _ilu
        # Load with qualified name so internal _load_admin_sibling works
        spec = _ilu.spec_from_file_location("admin_console.backend.app", str(BACKEND / "app.py"))
        mod = _ilu.module_from_spec(spec)
        mod.__package__ = "admin_console.backend"
        sys.modules["admin_console.backend.app"] = mod
        spec.loader.exec_module(mod)  # type: ignore
        return mod.app

# ---------------------------------------------------------------------------
# Admin API tests (verified JWT)
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient

@pytest.fixture
def client():
    app = _load_admin_app()
    return TestClient(app)

def test_valid_jwt_read_200(client):
    token = _make_wiki_jwt(tenant_id="acme", agent_id="agent:assistant:kim", scope="wiki:read", aud="wiki-fs", iss="control-plane")
    r = client.get("/v1/personal-wiki/notes", headers=_auth_header(token))
    # Should be 200 and owner should be from JWT, not header
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["owner"] == "employee:kim"
    assert "agent:assistant:kim" in data["vault_path"]

def test_valid_jwt_write_200(client):
    token = _make_wiki_jwt(tenant_id="acme", agent_id="agent:assistant:kim", scope="wiki:write", aud="wiki-fs", iss="control-plane")
    r = client.post(
        "/v1/personal-wiki/attachments",
        files={"file": ("hello.txt", b"hello wiki", "text/plain")},
        headers=_auth_header(token),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "vault_path" in data
    assert "agent:assistant:kim" in data["vault_path"]

def test_expired_jwt_401(client):
    token = _make_wiki_jwt(exp_delta_seconds=-60)
    r = client.get("/v1/personal-wiki/notes", headers=_auth_header(token))
    assert r.status_code == 401, r.text

def test_wrong_issuer_401(client):
    token = _make_wiki_jwt(iss="evil-issuer")
    r = client.get("/v1/personal-wiki/notes", headers=_auth_header(token))
    assert r.status_code == 401, r.text

def test_wrong_audience_401(client):
    token = _make_wiki_jwt(aud="evil-aud")
    r = client.get("/v1/personal-wiki/notes", headers=_auth_header(token))
    assert r.status_code == 401, r.text

def test_missing_scope_401(client):
    token = _make_wiki_jwt(extra={"scope": None})
    r = client.get("/v1/personal-wiki/notes", headers=_auth_header(token))
    assert r.status_code == 401, r.text

def test_invalid_scope_401(client):
    token = _make_wiki_jwt(scope="wiki:evil")
    r = client.get("/v1/personal-wiki/notes", headers=_auth_header(token))
    assert r.status_code == 401, r.text

def test_missing_tenant_401(client):
    token = _make_wiki_jwt(extra={"tenant_id": None})
    r = client.get("/v1/personal-wiki/notes", headers=_auth_header(token))
    assert r.status_code == 401, r.text

def test_missing_agent_401(client):
    token = _make_wiki_jwt(extra={"agent_id": None})
    r = client.get("/v1/personal-wiki/notes", headers=_auth_header(token))
    assert r.status_code == 401, r.text

def test_invalid_signature_401(client):
    token = _make_wiki_jwt(signing_key="wrong-key-32bytes-long-enough-not-match")
    r = client.get("/v1/personal-wiki/notes", headers=_auth_header(token))
    assert r.status_code == 401, r.text

def test_anon_rejected_401(client):
    # No Bearer, no X-User-Id fallback in prod path -> 401
    # In non-prod with fixture, X-User-Id might be allowed, but bare anon without any header should be 401
    # Remove any implicit headers
    r = client.get("/v1/personal-wiki/notes")
    assert r.status_code == 401, r.text

def test_cross_tenant_body_ignored(client):
    """Body / query tenant must be ignored; JWT tenant is authoritative. Request with JWT tenant acme but trying to list with different tenant via header mismatch should be 403 or owner stays acme."""
    token = _make_wiki_jwt(tenant_id="acme", agent_id="agent:assistant:kim", scope="wiki:read")
    # Try to inject X-Tenant-Id header with evil value - should be ignored or cause 403, but owner must remain acme
    r = client.get("/v1/personal-wiki/notes", headers={**_auth_header(token), "X-Tenant-Id": "evil-tenant"})
    # Per H3, tenant binding should reject mismatch if endpoint checks tenant binding. Our Wiki endpoint doesn't take tenant body param for notes, but we verify JWT tenant is used for vault_path.
    # So either 403 or 200 with vault_path still containing acme/expected owner. We assert that evil tenant does not change owner vault.
    if r.status_code == 200:
        data = r.json()
        # vault_path should NOT contain evil-tenant if it were using body; but our vault_path uses agent, not tenant string directly. Check owner remains kim
        assert data["owner"] == "employee:kim"
    else:
        assert r.status_code == 403, r.text

def test_cross_tenant_jwt_mismatch_via_search(client):
    # Same as above but search endpoint - trying to use JWT for acme to read other's data should not leak
    token = _make_wiki_jwt(tenant_id="acme", agent_id="agent:assistant:kim", scope="wiki:read")
    # Attempt to use JWT tenant acme but the server should still return only acme data (mock). This test ensures JWT tenant is respected.
    r = client.get("/v1/personal-wiki/search", params={"q": "hello"}, headers=_auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["owner"] == "employee:kim"

def test_path_traversal_403_notes(client):
    """Slug traversal via note_id query? For notes list, we test that vault helper rejects traversal in attachment filename? Alternatively test attachments with traversal filename should be sanitized or 403."""
    # We test that vault_path_for_note with traversal is rejected when using JWT context
    # Admin endpoint currently doesn't have slug param for notes, but attachments uses filename. We'll test uploading file with traversal name
    token = _make_wiki_jwt(scope="wiki:write")
    # Try to upload file with traversal name - should be sanitized or rejected, but must not escape vault
    r = client.post(
        "/v1/personal-wiki/attachments",
        files={"file": ("../../etc/passwd", b"evil", "text/plain")},
        headers=_auth_header(token),
    )
    assert r.status_code == 403, r.text
    assert "PATH_TRAVERSAL" in r.text

def test_header_fallback_rejected_without_jwt_in_prod(client, monkeypatch):
    """In production, X-User-Id header alone must NOT authenticate (no unverified JWT)."""
    monkeypatch.setenv("OAOS_ENV", "production")
    # Need to reload auth gate? For personal_wiki, is_production checks env at request time, so monkeypatch is enough
    r = client.get("/v1/personal-wiki/notes", headers={"X-User-Id": "employee:evil"})
    assert r.status_code == 401, f"production header fallback should be 401, got {r.status_code} {r.text}"
    monkeypatch.delenv("OAOS_ENV", raising=False)

def test_nonprod_explicit_fixture_allows_header(client, monkeypatch):
    """Non-prod with PYTEST_CURRENT_TEST (explicit fixture) allows X-User-Id header fallback for backwards compat."""
    # Ensure OAOS_ENV not production
    monkeypatch.delenv("OAOS_ENV", raising=False)
    # PYTEST_CURRENT_TEST is set by pytest automatically - our code should check it
    # So header should succeed in non-prod under pytest
    r = client.get("/v1/personal-wiki/notes", headers={"X-User-Id": "employee:testuser"})
    # This should be 200 because explicit test fixture allows header in non-prod
    assert r.status_code == 200, r.text
    assert r.json()["owner"] == "employee:testuser"

# ---------------------------------------------------------------------------
# Vault FS unit tests (direct)
# ---------------------------------------------------------------------------
def _load_pw_mod(name: str, rel: str):
    import importlib.util, pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    fp = root / "packages" / "personal-wiki" / rel
    spec = importlib.util.spec_from_file_location(name, str(fp))
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    spec.loader.exec_module(mod)  # type: ignore
    return mod

def test_vault_traversal_rejected():
    import importlib.util, pathlib, sys
    root = pathlib.Path(__file__).resolve().parents[1]
    pkg_root = root / "packages" / "personal-wiki"
    if str(pkg_root) not in sys.path:
        sys.path.insert(0, str(pkg_root))
    # load via file spec to avoid shadow
    auth_mod = _load_pw_mod("pw_auth_test", "personal_wiki/auth.py")
    verify_wiki_jwt = auth_mod.verify_wiki_jwt
    assert_vault_path_safe = auth_mod.assert_vault_path_safe

    token = _make_wiki_jwt(tenant_id="acme", agent_id="agent:assistant:kim", scope="wiki:write")
    payload = verify_wiki_jwt(token, required_scope="wiki:write")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "vault"
        # attempt traversal via slug
        with pytest.raises(Exception) as exc:
            # slug containing .. should be rejected via vault path safety - escapes notes dir
            # Use vault root as base, target escapes vault root via .. 
            assert_vault_path_safe(Path(root) / "notes" / ".." / ".." / "etc" / "passwd", Path(root))
        assert "traversal" in str(exc.value).lower() or "403" in str(exc.value)

def test_vault_cross_tenant_rejected():
    auth_mod = _load_pw_mod("pw_auth_test2", "personal_wiki/auth.py")
    verify_wiki_jwt = auth_mod.verify_wiki_jwt
    verify_tenant_agent_binding = auth_mod.verify_tenant_agent_binding

    token = _make_wiki_jwt(tenant_id="acme", agent_id="agent:assistant:kim", scope="wiki:read")
    payload = verify_wiki_jwt(token)
    with pytest.raises(Exception):
        verify_tenant_agent_binding(payload, requested_tenant="evil", requested_agent="agent:assistant:kim")

def test_vault_cross_agent_rejected():
    auth_mod = _load_pw_mod("pw_auth_test3", "personal_wiki/auth.py")
    verify_wiki_jwt = auth_mod.verify_wiki_jwt
    verify_tenant_agent_binding = auth_mod.verify_tenant_agent_binding

    token = _make_wiki_jwt(tenant_id="acme", agent_id="agent:assistant:kim", scope="wiki:read")
    payload = verify_wiki_jwt(token)
    with pytest.raises(Exception):
        verify_tenant_agent_binding(payload, requested_tenant="acme", requested_agent="agent:assistant:lee")

def test_vault_verify_wiki_jwt_scope_write_required():
    auth_mod = _load_pw_mod("pw_auth_test4", "personal_wiki/auth.py")
    verify_wiki_jwt = auth_mod.verify_wiki_jwt
    token_read = _make_wiki_jwt(scope="wiki:read")
    # write operation requires wiki:write, read token should fail
    with pytest.raises(Exception):
        verify_wiki_jwt(token_read, required_scope="wiki:write")
    # read operation accepts both read and write
    payload = verify_wiki_jwt(token_read, required_scope="wiki:read")
    assert payload["scope"] == "wiki:read"
    token_write = _make_wiki_jwt(scope="wiki:write")
    payload2 = verify_wiki_jwt(token_write, required_scope="wiki:read")
    assert payload2["scope"] == "wiki:write"
    payload3 = verify_wiki_jwt(token_write, required_scope="wiki:write")
    assert payload3["scope"] == "wiki:write"
