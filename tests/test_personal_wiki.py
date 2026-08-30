"""Personal Wiki API skeleton tests — 2 tests + vault path helper.

Keeps 541+2 passing when added.
"""
from __future__ import annotations
import sys
from pathlib import Path
import importlib.util
import os
import uuid
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"

def _load_admin_module(name: str, filename: str, bare_alias: str | None = None):
    added = False
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
        added = True
    spec = importlib.util.spec_from_file_location(name, str(BACKEND / filename))
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    sys.modules[name] = mod
    if bare_alias:
        sys.modules[bare_alias] = mod
    spec.loader.exec_module(mod)  # type: ignore
    return mod

# Ensure admin_app is loaded (it already mounts personal_wiki router lazily)
# Load personal_wiki directly to test vault helper regardless of app.py mount order
pw_mod = _load_admin_module("admin_personal_wiki", "personal_wiki.py", bare_alias="personal_wiki")
# Ensure app module loaded and router mounted
try:
    app_mod = sys.modules.get("admin_app")
    if app_mod is None:
        app_mod = _load_admin_module("admin_app", "app.py")
    admin_app = app_mod.app
except Exception:
    # fallback: try relative
    import admin_console.backend.app as app_mod2  # type: ignore

    admin_app = app_mod2.app

# Restore real personal_wiki package for subsequent tests (embed/e2e) that need vault/extractor/importer
try:
    _pkg_root = ROOT / "packages" / "personal-wiki"
    if str(_pkg_root) not in sys.path:
        sys.path.insert(0, str(_pkg_root))
    # admin file was registered as personal_wiki; replace with real package for later imports
    # Keep admin alias, but re-point personal_wiki to package
    if "personal_wiki" in sys.modules:
        # admin file currently shadows package; stash it and reload package
        _admin_mod = sys.modules["personal_wiki"]
        # ensure admin_personal_wiki still points to admin file
        sys.modules["admin_personal_wiki"] = _admin_mod
        del sys.modules["personal_wiki"]
        for k in list(sys.modules.keys()):
            if k.startswith("personal_wiki."):
                # don't delete admin's personal_wiki alias submodules (none)
                del sys.modules[k]
        # ensure package front of backend
        if str(BACKEND) in sys.path:
            sys.path.remove(str(BACKEND))
            sys.path.append(str(BACKEND))
        if str(_pkg_root) in sys.path:
            sys.path.remove(str(_pkg_root))
        sys.path.insert(0, str(_pkg_root))
        import importlib as _imp
        try:
            _imp.import_module("personal_wiki")
        except Exception:
            pass
except Exception:
    pass

from fastapi.testclient import TestClient


def _client():
    return TestClient(admin_app)


# Helper for production JWT (uses conftest unified signing key)
TEST_SIGNING_KEY = os.environ.get("OAOS_SIGNING_KEY") or "test-unified-oaos-signing-key-32bytes-long-enough!!"


def _make_wiki_jwt(sub="employee:testuser", tenant_id="default", agent_id="agent:assistant:testuser", scope="wiki:read", exp_delta=300, iss="control-plane", aud="wiki-fs"):
    from jose import jwt as _jwt

    now = datetime.now(timezone.utc)
    payload = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "scope": scope,
        "exp": int((now + timedelta(seconds=exp_delta)).timestamp()),
        "iat": int(now.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return _jwt.encode(payload, TEST_SIGNING_KEY, algorithm="HS256")


def test_personal_wiki_vault_path_helper_owner_isolated():
    # employee:xxx -> agent:xxx isolation
    p1 = pw_mod.get_vault_path("employee:kim")
    p2 = pw_mod.get_vault_path("employee:lee")
    assert "agent:assistant:kim" in p1
    assert "agent:assistant:lee" in p2
    assert p1 != p2
    # personal_wiki segment present
    assert "personal_wiki" in p1
    # note path helper
    note_path = pw_mod.vault_path_for_note("employee:kim", "note_123")
    assert note_path.startswith(p1)
    assert "note_123" in note_path


def test_personal_wiki_endpoints_mock_when_db_not_configured(tmp_path, monkeypatch):
    """Non-prod with DB not configured and empty vault -> mock fallback allowed (explicit non-production env)."""
    vault_root = tmp_path / "vault-mock"
    monkeypatch.setenv("OAOS_WIKI_VAULT", str(vault_root))
    # explicitly force non-production environment for mock fixture
    monkeypatch.setenv("OAOS_ENV", "test")
    for k in ("ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    # also clear prod-sensitive DB variants that might affect _is_db_configured
    monkeypatch.delenv("VAULT_BACKEND", raising=False)
    monkeypatch.delenv("VAULT_ADDR", raising=False)

    c = _client()
    # GET /v1/personal-wiki/notes -> mock notes, owner via X-User-Id (allowed in non-prod fixture)
    r = c.get("/v1/personal-wiki/notes", headers={"X-User-Id": "employee:testuser"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "notes" in data
    assert data["mock"] is True
    assert data["owner"] == "employee:testuser"
    assert "vault_path" in data

    # GET /v1/personal-wiki/search?q=hello -> mock search
    r2 = c.get("/v1/personal-wiki/search", params={"q": "hello"}, headers={"X-User-Id": "employee:testuser"})
    assert r2.status_code == 200, r2.text
    d2 = r2.json()
    assert d2["query"] == "hello"
    assert "results" in d2
    assert d2["mock"] is True

    # POST /v1/personal-wiki/attachments -> vault FS persist (mock False when FS works, even in non-prod)
    # When vault FS is configured via OAOS_WIKI_VAULT, upload succeeds via FS, mock=False
    # Use fresh tenant/agent to ensure FS write path works
    r3 = c.post(
        "/v1/personal-wiki/attachments",
        files={"file": ("hello.txt", b"hello world personal wiki", "text/plain")},
        headers={"X-User-Id": "employee:testuser"},
    )
    assert r3.status_code == 200, r3.text
    d3 = r3.json()
    assert "attachment_id" in d3
    assert "extracted_text" in d3
    assert "note" in d3
    assert "journal" in d3
    # upload writes to vault FS, so mock is False (real FS result) — but still 200 in non-prod
    assert d3["mock"] is False
    assert "vault_path" in d3
    # owner isolated vault path contains agent id or tenant iso path
    assert "agent:assistant:testuser" in d3["vault_path"] or "testuser" in d3["vault_path"]


def test_personal_wiki_production_fails_closed_when_db_not_configured(tmp_path, monkeypatch):
    """Production with DB not configured and empty vault -> fail-closed 503 (no mock)."""
    vault_root = tmp_path / "vault-prod-failclosed"
    monkeypatch.setenv("OAOS_WIKI_VAULT", str(vault_root))
    monkeypatch.setenv("OAOS_ENV", "production")
    for k in ("ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("VAULT_BACKEND", raising=False)
    monkeypatch.delenv("VAULT_ADDR", raising=False)

    c = _client()
    # Use verified JWT — X-User-Id alone would be 401 in production, 503 is the DB-missing fail-closed
    token_read = _make_wiki_jwt(sub="employee:produser", tenant_id="acme", agent_id="agent:assistant:produser", scope="wiki:read")
    r = c.get("/v1/personal-wiki/notes", headers={"Authorization": f"Bearer {token_read}"})
    assert r.status_code == 503, f"production notes without DB should be 503, got {r.status_code} {r.text}"
    assert "not configured" in r.text.lower() or "mock fallback" in r.text.lower()

    # search also fails closed in production when DB not configured and FS empty
    r2 = c.get("/v1/personal-wiki/search", params={"q": "hello"}, headers={"Authorization": f"Bearer {token_read}"})
    assert r2.status_code == 503, f"production search without DB should be 503, got {r2.status_code} {r2.text}"

    # attachment upload with write scope still writes to FS in production (vault FS is available), so not 503
    # Verify that production upload with valid FS does NOT return mock True
    token_write = _make_wiki_jwt(sub="employee:produser", tenant_id="acme", agent_id="agent:assistant:produser", scope="wiki:write")
    r3 = c.post(
        "/v1/personal-wiki/attachments",
        files={"file": ("hello.txt", b"hello production", "text/plain")},
        headers={"Authorization": f"Bearer {token_write}"},
    )
    # When vault FS succeeds, production returns 200 with mock False, not 503 and not mock
    if r3.status_code == 200:
        assert r3.json().get("mock") is not True
    else:
        # if vault somehow fails, should be 503
        assert r3.status_code == 503, r3.text
