"""LLM Provider Vault — Fernet encryption + secret_ref + DB fallback.

- Fernet encrypt/decrypt roundtrip
- OAOS_VAULT_KEY / VAULT_ENCRYPTION_KEY env loading
- encrypted_api_key + secret_ref (vault://admin_llm_providers/{id}/api_key)
- GET masking (****)
- creation/update 암호화 저장, raw never leaked
- Alembic 009 columns 활용, DB-backed (SQLAlchemy) with in-memory fallback
"""
from __future__ import annotations

import os
import sys
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"


def _load_admin_module(name: str, filename: str, bare_alias: str | None = None):
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location(name, str(BACKEND / filename))
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    sys.modules[name] = mod
    if bare_alias:
        sys.modules[bare_alias] = mod
    spec.loader.exec_module(mod)  # type: ignore
    return mod


# Load fresh modules — ensure llm_providers sees cryptography
auth_mod = _load_admin_module("admin_auth_vault", "auth.py", bare_alias="auth")
# Ensure llm_providers loads after auth
llm_mod = _load_admin_module("admin_llm_providers_vault", "llm_providers.py", bare_alias="llm_providers")
app_mod = _load_admin_module("admin_app_vault", "app.py")
# Remove BACKEND from path front to avoid polluting other tests
if str(BACKEND) in sys.path:
    sys.path.remove(str(BACKEND))

admin_app = app_mod.app


@pytest.fixture(autouse=True)
def isolate():
    # deterministic vault key for tests
    os.environ["OAOS_VAULT_KEY"] = "test-vault-key-for-llm-provider-32bytes!!"
    # Force llm mode — vault tests require provider CRUD, which is blocked in hermes mode (409)
    os.environ["OAOS_RUNTIME_MODE"] = "llm"
    _orig_guards = {}
    try:
        import sys
        # Ensure runtime_mode module is loaded and set to llm (both bare and canonical)
        for mod_name in ("runtime_mode", "admin_console.backend.runtime_mode", "admin_llm_providers_vault"):
            try:
                if mod_name not in sys.modules:
                    if mod_name.startswith("admin_console"):
                        # canonical will be created when app loads llm_providers; ensure via import
                        continue
                    import importlib.util
                    from pathlib import Path
                    be = Path(__file__).resolve().parents[1] / "admin-console" / "backend"
                    spec = importlib.util.spec_from_file_location(mod_name, str(be / "runtime_mode.py"))
                    rm = importlib.util.module_from_spec(spec)
                    sys.modules[mod_name] = rm
                    spec.loader.exec_module(rm)
                rm = sys.modules.get(mod_name)
                if rm and hasattr(rm, "set_mode") and hasattr(rm, "RuntimeMode"):
                    rm.set_mode(rm.RuntimeMode.llm)
            except Exception:
                pass
        # Directly set canonical runtime_mode if app has already created it
        for canon in ("admin_console.backend.runtime_mode", "runtime_mode"):
            try:
                rm = sys.modules.get(canon)
                if rm and hasattr(rm, "set_mode"):
                    rm.set_mode(rm.RuntimeMode.llm)  # type: ignore
            except Exception:
                pass
        # Patch guards on all llm_providers variants (private + canonical + bare)
        for target in (llm_mod, sys.modules.get("llm_providers"), sys.modules.get("admin_console.backend.llm_providers")):
            try:
                if target is not None and hasattr(target, "_check_hermes_mode_guard"):
                    _orig_guards[id(target)] = target._check_hermes_mode_guard
                    target._check_hermes_mode_guard = lambda: None  # type: ignore
            except Exception:
                pass
    except Exception:
        pass
    # clear state (both private and canonical)
    try:
        auth_mod.clear_users()
    except Exception:
        pass
    try:
        llm_mod.clear_providers()
    except Exception:
        pass
    for canon in ("admin_console.backend.llm_providers", "llm_providers"):
        try:
            m = sys.modules.get(canon)
            if m and hasattr(m, "clear_providers"):
                m.clear_providers()
        except Exception:
            pass
    for canon in ("admin_console.backend.auth", "auth"):
        try:
            m = sys.modules.get(canon)
            if m and m is not auth_mod and hasattr(m, "clear_users"):
                m.clear_users()
        except Exception:
            pass
    # Alias private module dicts to canonical so writes via app are visible to assertions
    try:
        import sys as _sys
        canon = _sys.modules.get("admin_console.backend.llm_providers")
        if canon is not None:
            for attr in ("_providers", "_encrypted_store", "_secret_refs", "_quota_store", "_quota_window_counts"):
                try:
                    setattr(llm_mod, attr, getattr(canon, attr))
                except Exception:
                    pass
            # also alias helpers so get_encrypted_api_key reads canonical store
            for fn in ("get_encrypted_api_key", "get_provider", "list_providers"):
                try:
                    if hasattr(canon, fn):
                        setattr(llm_mod, fn, getattr(canon, fn))
                except Exception:
                    pass
    except Exception:
        pass
    yield
    # restore guards
    for target in (llm_mod, sys.modules.get("llm_providers"), sys.modules.get("admin_console.backend.llm_providers")):
        try:
            if target is not None and id(target) in _orig_guards:
                target._check_hermes_mode_guard = _orig_guards[id(target)]
        except Exception:
            pass
    try:
        llm_mod.clear_providers()
    except Exception:
        pass
    try:
        auth_mod.clear_users()
    except Exception:
        pass


def _client():
    return TestClient(admin_app)


def _login(email="admin@openit.co.kr", password="Admin123!"):
    c = _client()
    r = c.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Fernet crypto unit
# ---------------------------------------------------------------------------
def test_fernet_roundtrip():
    plain = "sk-test-1234567890abcdef"
    enc = llm_mod._encrypt_api_key(plain)
    assert enc != plain
    assert enc != ""
    # encrypted should be Fernet token (base64 urlsafe, starts with gAAAAA)
    assert enc.startswith("gAAAAA")
    dec = llm_mod._decrypt_api_key(enc)
    assert dec == plain


def test_fernet_env_key_loading():
    # OAOS_VAULT_KEY is set in fixture
    enc1 = llm_mod._encrypt_api_key("hello-key-1")
    dec1 = llm_mod._decrypt_api_key(enc1)
    assert dec1 == "hello-key-1"
    # Change to VAULT_ENCRYPTION_KEY fallback: clear OAOS_VAULT_KEY, set VAULT_ENCRYPTION_KEY
    old_oaos = os.environ.pop("OAOS_VAULT_KEY", None)
    os.environ["VAULT_ENCRYPTION_KEY"] = "fallback-vault-key-32bytes-test!!"
    llm_mod._fernet_cache.clear()
    enc2 = llm_mod._encrypt_api_key("hello-key-2")
    dec2 = llm_mod._decrypt_api_key(enc2)
    assert dec2 == "hello-key-2"
    # wrong key should fail decrypt
    os.environ["VAULT_ENCRYPTION_KEY"] = "different-key-should-fail-decrypt!!"
    llm_mod._fernet_cache.clear()
    dec_fail = llm_mod._decrypt_api_key(enc2)
    assert dec_fail is None or dec_fail != "hello-key-2"
    # restore
    if old_oaos is not None:
        os.environ["OAOS_VAULT_KEY"] = old_oaos
    os.environ.pop("VAULT_ENCRYPTION_KEY", None)
    llm_mod._fernet_cache.clear()
    os.environ["OAOS_VAULT_KEY"] = "test-vault-key-for-llm-provider-32bytes!!"


def test_fernet_decrypt_invalid():
    assert llm_mod._decrypt_api_key(None) is None
    assert llm_mod._decrypt_api_key("") is None
    assert llm_mod._decrypt_api_key("not-a-fernet-token") is None


# ---------------------------------------------------------------------------
# 2. secret_ref format
# ---------------------------------------------------------------------------
def test_secret_ref_format():
    assert llm_mod._make_secret_ref("llm_abc123") == "vault://admin_llm_providers/llm_abc123/api_key"


# ---------------------------------------------------------------------------
# 3. CRUD with encryption + masking
# ---------------------------------------------------------------------------
def test_create_masks_and_encrypts():
    token = _login()
    c = _client()
    h = _auth(token)
    raw_key = "sk-claude-test-1234567890abcdef"
    r = c.post("/v1/llm/providers", json={"provider": "claude", "apiKey": raw_key, "model": "claude-3"}, headers=h)
    assert r.status_code == 201, r.text
    data = r.json()
    pid = data["id"]
    # response must be masked, not raw
    assert data["apiKey"] != raw_key
    assert "***" in data["apiKey"]
    assert data["api_key"] != raw_key
    assert "***" in data["api_key"]
    # raw must never appear in response body
    assert raw_key not in r.text
    # encrypted storage must exist and be Fernet token
    enc = llm_mod.get_encrypted_api_key(pid)
    assert enc is not None
    assert enc != raw_key
    assert enc.startswith("gAAAAA")
    # decrypt should yield original
    assert llm_mod.decrypt_api_key_for_test(enc) == raw_key
    # secret_ref format
    sref = llm_mod.get_secret_ref(pid)
    assert sref == f"vault://admin_llm_providers/{pid}/api_key"
    # also exposed in response
    assert data.get("secret_ref") == sref


def test_get_masks():
    token = _login()
    c = _client()
    h = _auth(token)
    raw = "sk-gemini-1234567890abcdefXXXX"
    r = c.post("/v1/llm/providers", json={"provider": "gemini", "apiKey": raw}, headers=h)
    pid = r.json()["id"]
    # GET single
    r2 = c.get(f"/v1/llm/providers/{pid}", headers=h)
    assert r2.status_code == 200
    assert raw not in r2.text
    assert "***" in r2.json()["apiKey"]
    # LIST
    r3 = c.get("/v1/llm/providers", headers=h)
    assert r3.status_code == 200
    items = r3.json()["providers"]
    found = [x for x in items if x["id"] == pid]
    assert len(found) == 1
    assert "***" in found[0]["apiKey"]
    assert raw not in r3.text


def test_update_re_encrypts():
    token = _login()
    c = _client()
    h = _auth(token)
    raw1 = "sk-codex-orig-1234567890abcd"
    r = c.post("/v1/llm/providers", json={"provider": "codex", "apiKey": raw1}, headers=h)
    pid = r.json()["id"]
    enc1 = llm_mod.get_encrypted_api_key(pid)
    # update with new key
    raw2 = "sk-codex-new-9999999999abcd"
    r2 = c.patch(f"/v1/llm/providers/{pid}", json={"apiKey": raw2}, headers=h)
    assert r2.status_code == 200
    assert "***" in r2.json()["apiKey"]
    assert raw2 not in r2.text
    enc2 = llm_mod.get_encrypted_api_key(pid)
    assert enc2 != enc1
    assert llm_mod.decrypt_api_key_for_test(enc2) == raw2
    # patch with masked placeholder should NOT change key
    masked = r2.json()["apiKey"]
    r3 = c.patch(f"/v1/llm/providers/{pid}", json={"apiKey": masked}, headers=h)
    assert r3.status_code == 200
    enc3 = llm_mod.get_encrypted_api_key(pid)
    assert enc3 == enc2  # unchanged


def test_create_opencode_ollama_no_api_key():
    token = _login()
    c = _client()
    h = _auth(token)
    # opencode requires path, no apiKey
    r = c.post("/v1/llm/providers", json={"provider": "opencode", "path": "/opt/opencode"}, headers=h)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert llm_mod.get_encrypted_api_key(pid) is None or llm_mod.get_encrypted_api_key(pid) == ""
    assert llm_mod.get_secret_ref(pid) is None  # no secret_ref when no apiKey
    # ollama requires url
    r2 = c.post("/v1/llm/providers", json={"provider": "ollama", "url": "http://localhost:11434"}, headers=h)
    assert r2.status_code == 201, r2.text


def test_delete_clears_encrypted():
    token = _login()
    c = _client()
    h = _auth(token)
    r = c.post("/v1/llm/providers", json={"provider": "claude", "apiKey": "sk-delete-test-12345678"}, headers=h)
    pid = r.json()["id"]
    assert llm_mod.get_encrypted_api_key(pid) is not None
    r2 = c.delete(f"/v1/llm/providers/{pid}", headers=h)
    assert r2.status_code == 200
    assert llm_mod.get_encrypted_api_key(pid) is None
    r3 = c.get(f"/v1/llm/providers/{pid}", headers=h)
    assert r3.status_code == 404


def test_test_and_toggle_persist():
    token = _login()
    c = _client()
    h = _auth(token)
    r = c.post("/v1/llm/providers", json={"provider": "claude", "apiKey": "sk-toggle-test-12345678"}, headers=h)
    pid = r.json()["id"]
    # test
    r2 = c.post(f"/v1/llm/providers/{pid}/test", headers=h)
    assert r2.status_code == 200
    assert r2.json()["status"] == "ok"
    # toggle
    before = c.get(f"/v1/llm/providers/{pid}", headers=h).json()["enabled"]
    r3 = c.post(f"/v1/llm/providers/{pid}/toggle", headers=h)
    assert r3.status_code == 200
    assert r3.json()["enabled"] != before


# ---------------------------------------------------------------------------
# 4. DB-backed with sqlite memory (OAOS_DATABASE_URL)
# ---------------------------------------------------------------------------
def test_db_backed_sqlite_memory():
    # Use sqlite memory for this test — set OAOS_DATABASE_URL
    import tempfile

    import pathlib as _pl

    # use a unique temp file to avoid cross-run pollution (previous version used fixed /tmp path)
    _tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _tmp_path = _pl.Path(_tf.name)
    _tf.close()
    db_url = f"sqlite:////{_tmp_path}"
    old_url = os.environ.get("OAOS_DATABASE_URL")
    old_db = os.environ.get("DATABASE_URL")
    os.environ["OAOS_DATABASE_URL"] = db_url
    # reset engine cache so new URL is picked up — must reset both the
    # private test alias (llm_mod) and the canonical app module that
    # actually serves requests. Use _reset_db_cache() when available so
    # _db_cached_url is also cleared; fallback to manual clear for compat.
    for _m in (llm_mod, sys.modules.get("admin_console.backend.llm_providers"), sys.modules.get("llm_providers")):
        try:
            if _m is not None:
                if hasattr(_m, "_reset_db_cache"):
                    _m._reset_db_cache()
                else:
                    if hasattr(_m, "_db_engine"):
                        _m._db_engine = None
                    if hasattr(_m, "_db_session_factory"):
                        _m._db_session_factory = None
                    if hasattr(_m, "_db_cached_url"):
                        _m._db_cached_url = None
                if hasattr(_m, "_fernet_cache"):
                    try:
                        _m._fernet_cache.clear()
                    except Exception:
                        pass
        except Exception:
            pass
    llm_mod._fernet_cache.clear()
    os.environ["OAOS_VAULT_KEY"] = "test-vault-key-for-llm-provider-32bytes!!"

    # clear providers (also clears DB) — after cache reset, _get_session_factory
    # will rebuild the engine for the new sqlite URL and _db_ensure_table
    # will be called on the current engine (idempotent)
    llm_mod.clear_providers()
    try:
        canon = sys.modules.get("admin_console.backend.llm_providers")
        if canon is not None and hasattr(canon, "clear_providers"):
            canon.clear_providers()
    except Exception:
        pass

    try:
        token = _login()
        c = _client()
        h = _auth(token)
        raw = "sk-db-test-1234567890abcdef"
        r = c.post("/v1/llm/providers", json={"provider": "claude", "apiKey": raw, "model": "claude-3"}, headers=h)
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        assert "***" in r.json()["apiKey"]
        assert raw not in r.text

        # Verify DB row has encrypted_api_key and secret_ref, not raw
        try:
            from sqlalchemy import create_engine, text

            sync_url = llm_mod._normalize_sync_url(db_url)
            eng = create_engine(sync_url)
            with eng.connect() as conn:
                row = conn.execute(text("SELECT id, provider, encrypted_api_key, secret_ref, vault_backend FROM admin_llm_providers WHERE id=:id"), {"id": pid}).fetchone()
                assert row is not None, "DB row not found"
                assert row[2] is not None and row[2].startswith("gAAAAA"), f"encrypted_api_key not Fernet: {row[2]}"
                assert row[2] != raw
                assert raw not in str(row[2])
                assert row[3] == f"vault://admin_llm_providers/{pid}/api_key"
                assert row[4] == "fernet"
            eng.dispose()
        except Exception as e:
            pytest.fail(f"DB verification failed: {e}")

        # GET via DB should still mask
        r2 = c.get(f"/v1/llm/providers/{pid}", headers=h)
        assert r2.status_code == 200
        assert raw not in r2.text
        assert "***" in r2.json()["apiKey"]

        # LIST via DB
        r3 = c.get("/v1/llm/providers", headers=h)
        assert r3.status_code == 200
        assert any(x["id"] == pid for x in r3.json()["providers"])
    finally:
        # always cleanup even if verification fails
        try:
            llm_mod.clear_providers()
        except Exception:
            pass
        try:
            _tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        # also try legacy fixed path cleanup for old runs
        try:
            Path("/tmp/test_llm_provider_vault.db").unlink(missing_ok=True)
        except Exception:
            pass
        if old_url is not None:
            os.environ["OAOS_DATABASE_URL"] = old_url
        else:
            os.environ.pop("OAOS_DATABASE_URL", None)
        if old_db is not None:
            os.environ["DATABASE_URL"] = old_db
        else:
            os.environ.pop("DATABASE_URL", None)
        for _m in (llm_mod, sys.modules.get("admin_console.backend.llm_providers"), sys.modules.get("llm_providers")):
            try:
                if _m is not None:
                    if hasattr(_m, "_reset_db_cache"):
                        _m._reset_db_cache()
                    else:
                        if hasattr(_m, "_db_engine"):
                            _m._db_engine = None
                        if hasattr(_m, "_db_session_factory"):
                            _m._db_session_factory = None
                        if hasattr(_m, "_db_cached_url"):
                            _m._db_cached_url = None
                    if hasattr(_m, "_fernet_cache"):
                        try:
                            _m._fernet_cache.clear()
                        except Exception:
                            pass
            except Exception:
                pass
        try:
            llm_mod._fernet_cache.clear()
        except Exception:
            pass
        os.environ["OAOS_VAULT_KEY"] = "test-vault-key-for-llm-provider-32bytes!!"
        try:
            llm_mod.clear_providers()
        except Exception:
            pass
        try:
            canon = sys.modules.get("admin_console.backend.llm_providers")
            if canon is not None and hasattr(canon, "clear_providers"):
                canon.clear_providers()
        except Exception:
            pass
        # also ensure auth DB state is clean for following tests
        try:
            for mod in [sys.modules.get("admin_console.backend.auth"), sys.modules.get("auth")]:
                if mod and hasattr(mod, "clear_users"):
                    mod.clear_users()
        except Exception:
            pass


def test_009_migration_columns_exist():
    """Verify Alembic 009 created expected columns."""
    path = ROOT / "alembic" / "versions" / "009_admin_llm_providers.py"
    assert path.exists()
    content = path.read_text()
    assert "encrypted_api_key" in content
    assert "secret_ref" in content
    assert "vault_backend" in content
    assert "admin_llm_providers" in content
    # ORM should have same columns
    from security.models.orm import AdminLLMProviderORM

    cols = {c.key for c in AdminLLMProviderORM.__table__.columns}
    assert "encrypted_api_key" in cols
    assert "secret_ref" in cols
    assert "vault_backend" in cols
