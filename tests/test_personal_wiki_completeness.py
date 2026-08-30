"""Failing tests for Personal Wiki completeness slice — path consistency, extractor, vault FS, memory service, production fail-closed."""
from __future__ import annotations
import os, sys, tempfile, uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
import importlib.util, types, importlib.machinery

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"
PKG_WIKI = ROOT / "packages" / "personal-wiki"
TEST_SIGNING_KEY = os.environ.get("OAOS_SIGNING_KEY") or "test-unified-oaos-signing-key-32bytes-long-enough!!"
for k in ("OAOS_SIGNING_KEY","OAOS_SECURITY_SERVICE_SIGNING_KEY","JWT_SIGNING_KEY","ADMIN_JWT_SECRET","OAOS_JWT_SIGNING_KEY"):
    os.environ[k] = TEST_SIGNING_KEY

# Ensure package wiki on path with priority over backend/personal_wiki.py
if str(PKG_WIKI) not in sys.path:
    sys.path.insert(0, str(PKG_WIKI))

def _make_wiki_jwt(sub="employee:kim", tenant_id="acme", agent_id="agent:assistant:kim", scope="wiki:read", exp_delta=300, iss="control-plane", aud="wiki-fs"):
    from jose import jwt
    now = datetime.now(timezone.utc)
    payload = {"iss": iss, "aud": aud, "sub": sub, "tenant_id": tenant_id, "agent_id": agent_id, "scope": scope, "exp": int((now+timedelta(seconds=exp_delta)).timestamp()), "iat": int(now.timestamp()), "jti": uuid.uuid4().hex}
    return jwt.encode(payload, TEST_SIGNING_KEY, algorithm="HS256")

def _load_admin_app():
    for pkg in ("admin_console","admin_console.backend"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg); m.__path__=[]; m.__spec__=importlib.machinery.ModuleSpec(pkg, None, is_package=True); sys.modules[pkg]=m
    try:
        import admin_console.backend.app as app_mod
        return app_mod.app
    except Exception:
        spec = importlib.util.spec_from_file_location("admin_console.backend.app", str(BACKEND/"app.py"))
        mod = importlib.util.module_from_spec(spec); mod.__package__="admin_console.backend"; sys.modules["admin_console.backend.app"]=mod; spec.loader.exec_module(mod); return mod.app

from fastapi.testclient import TestClient

def _client():
    app = _load_admin_app()
    return TestClient(app)

def test_attachment_upload_uses_extractor_and_vault_fs(tmp_path, monkeypatch):
    """Attachment upload must persist to owner-isolated vault FS and use extractor (not just utf8 slice)."""
    vault_root = tmp_path / "vault-attach"
    monkeypatch.setenv("OAOS_WIKI_VAULT", str(vault_root))
    # Ensure DB env unset so we test FS path not mock-DB
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("OAOS_ENV", raising=False)
    token = _make_wiki_jwt(sub="employee:alice", tenant_id="acme", agent_id="agent:assistant:alice", scope="wiki:write")
    c = _client()
    r = c.post("/v1/personal-wiki/attachments", files={"file": ("hello.txt", b"hello world personal wiki extractor test", "text/plain")}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code==200, r.text
    data = r.json()
    # must NOT be mock when vault FS works (even if DB unset)
    # In non-prod we may still allow mock flag but vault_path must be tenant/agent isolated and file must exist
    assert "vault_path" in data
    assert "acme" in data["vault_path"] or "agent:assistant:alice" in data["vault_path"], f"vault_path not owner-isolated: {data['vault_path']}"
    # Check extractor was used: extracted_text should contain full content
    assert "hello world" in data.get("extracted_text",""), "extractor not used"
    # Verify file actually persisted under tenant/agent vault
    # Look for attachments dir
    owner_attachments = list(vault_root.rglob("hello.txt"))
    # also check via vault_path_for_tenant_agent pattern
    assert len(owner_attachments) >= 1 or (vault_root / "acme" / "agent:assistant:alice" / "attachments").exists(), f"attachment not persisted to FS, vault contents: {list(vault_root.rglob('*'))}"
    # Verify note/journal created on FS (notes dir)
    notes = list((vault_root / "acme" / "agent:assistant:alice" / "notes").rglob("*.md")) if (vault_root / "acme" / "agent:assistant:alice" / "notes").exists() else list(vault_root.rglob("*.md"))
    # journal should also exist
    journals = list(vault_root.rglob("journal/*.md")) + list((vault_root / "acme" / "agent:assistant:alice" / "journal").rglob("*.md"))
    # At least one of notes/journal should exist if wiring is real
    assert len(notes) + len(journals) >= 1, f"no note/journal created on FS: notes={notes} journals={journals} vault={list(vault_root.rglob('*'))}"

def test_note_listing_owner_isolated_uses_vault_fs(tmp_path, monkeypatch):
    """Listing notes must read from vault FS owner-isolated, not return global mock containing other owners."""
    vault_root = tmp_path / "vault-list"
    monkeypatch.setenv("OAOS_WIKI_VAULT", str(vault_root))
    monkeypatch.delenv("DATABASE_URL", raising=False); monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    # Pre-create two owners' notes via vault API directly
    # Use tenant/agent isolated vault
    if str(PKG_WIKI) not in sys.path:
        sys.path.insert(0, str(PKG_WIKI))
    from personal_wiki.vault import vault_path_for_tenant_agent, ensure_vault_dirs
    # Create note for alice (acme)
    alice_root = vault_path_for_tenant_agent("acme", "agent:assistant:alice", vault_root=vault_root)
    # vault_path_for_tenant_agent returns full path including suffix? Without suffix returns base/tenant/agent
    # The above returns vault_root/acme/agent:assistant:alice — use that as owner root
    # Ensure dirs and create a note file
    notes_alice = alice_root / "notes"
    notes_alice.mkdir(parents=True, exist_ok=True)
    (notes_alice / "alice-note.md").write_text("# Alice Note\nsecret alice", encoding="utf-8")
    bob_root = vault_path_for_tenant_agent("acme", "agent:assistant:bob", vault_root=vault_root)
    notes_bob = bob_root / "notes"
    notes_bob.mkdir(parents=True, exist_ok=True)
    (notes_bob / "bob-note.md").write_text("# Bob Note\nsecret bob", encoding="utf-8")
    token_alice = _make_wiki_jwt(sub="employee:alice", tenant_id="acme", agent_id="agent:assistant:alice", scope="wiki:read")
    c = _client()
    r = c.get("/v1/personal-wiki/notes", headers={"Authorization": f"Bearer {token_alice}"})
    assert r.status_code==200, r.text
    data = r.json()
    notes = data.get("notes", [])
    # Must NOT contain bob's note => owner isolation
    texts = " ".join([n.get("title","")+n.get("content","")+n.get("id","") for n in notes])
    assert "bob-note" not in texts.lower() and "secret bob" not in texts, f"cross-owner leakage: {notes}"
    # Should contain alice's note if FS wiring is real (not mock)
    assert any("alice" in (n.get("title","")+n.get("content","")+n.get("id","")).lower() for n in notes), f"alice note not found in listing (mock?): {notes}"

def test_search_owner_isolated(tmp_path, monkeypatch):
    """Search must be owner-isolated and not return other tenant's hits."""
    vault_root = tmp_path / "vault-search"
    monkeypatch.setenv("OAOS_WIKI_VAULT", str(vault_root))
    monkeypatch.delenv("DATABASE_URL", raising=False); monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    from personal_wiki.vault import vault_path_for_tenant_agent
    alice_root = vault_path_for_tenant_agent("acme", "agent:assistant:alice", vault_root=vault_root)
    (alice_root / "notes").mkdir(parents=True, exist_ok=True)
    (alice_root / "notes" / "alice-hello.md").write_text("hello alice unique query xyz", encoding="utf-8")
    bob_root = vault_path_for_tenant_agent("acme", "agent:assistant:bob", vault_root=vault_root)
    (bob_root / "notes").mkdir(parents=True, exist_ok=True)
    (bob_root / "notes" / "bob-hello.md").write_text("hello bob unique", encoding="utf-8")
    token_alice = _make_wiki_jwt(sub="employee:alice", tenant_id="acme", agent_id="agent:assistant:alice", scope="wiki:read")
    c = _client()
    r = c.get("/v1/personal-wiki/search", params={"q":"xyz"}, headers={"Authorization": f"Bearer {token_alice}"})
    assert r.status_code==200, r.text
    d = r.json()
    results = d.get("results", [])
    txt = " ".join([str(x) for x in results]).lower()
    assert "bob" not in txt, f"cross-owner search leakage: {results}"
    # Should find alice's note via FS search fallback
    assert len(results) >= 1, f"search should find alice note, got 0: {d}"
    assert "xyz" in txt or "alice" in txt

def test_production_no_mock_fallback(tmp_path, monkeypatch):
    """In production, missing DB/vault must NOT return mock 200 — must fail-closed 401/503."""
    vault_root = tmp_path / "vault-prod"
    monkeypatch.setenv("OAOS_WIKI_VAULT", str(vault_root))
    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False); monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    # Ensure signing key still valid for production
    token = _make_wiki_jwt(sub="employee:prod", tenant_id="acme", agent_id="agent:assistant:prod", scope="wiki:read")
    c = _client()
    r = c.get("/v1/personal-wiki/notes", headers={"Authorization": f"Bearer {token}"})
    # In production with no DB, should NOT return mock:true 200; should be 503 or at least not mock
    if r.status_code == 200:
        data = r.json()
        assert data.get("mock") is not True, f"production should not return mock fallback: {data}"
        # If 200, it must be real FS result, not mock
    else:
        assert r.status_code in (503, 500, 401), r.text
    # Also test upload in production without DB should not be mock
    token_w = _make_wiki_jwt(sub="employee:prod", tenant_id="acme", agent_id="agent:assistant:prod", scope="wiki:write")
    r2 = c.post("/v1/personal-wiki/attachments", files={"file": ("a.txt", b"hello", "text/plain")}, headers={"Authorization": f"Bearer {token_w}"})
    if r2.status_code == 200:
        assert r2.json().get("mock") is not True, f"production upload should not be mock: {r2.json()}"

def test_path_consistency_uses_tenant_agent_vault(tmp_path, monkeypatch):
    """Path consistency: vault_path must include tenant and agent, derived from verified JWT, not from X-User-Id."""
    vault_root = tmp_path / "vault-path"
    monkeypatch.setenv("OAOS_WIKI_VAULT", str(vault_root))
    monkeypatch.delenv("DATABASE_URL", raising=False); monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    token = _make_wiki_jwt(sub="employee:kim", tenant_id="tenantX", agent_id="agent:assistant:kim", scope="wiki:write")
    c = _client()
    # X-Tenant-Id mismatch must be rejected 403 (H3 tenant binding), not honored
    r = c.post("/v1/personal-wiki/attachments", files={"file": ("safe.txt", b"content", "text/plain")}, headers={"Authorization": f"Bearer {token}", "X-User-Id": "employee:evil", "X-Tenant-Id": "evil-tenant"})
    assert r.status_code==403, f"tenant mismatch should be 403, got {r.status_code} {r.text}"
    assert "tenant mismatch" in r.text.lower() or "403" in r.text
    # Without mismatch headers, vault_path must be tenantX isolated
    r2 = c.post("/v1/personal-wiki/attachments", files={"file": ("safe2.txt", b"content2", "text/plain")}, headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code==200, r2.text
    vp = r2.json().get("vault_path","")
    assert "tenantX" in vp, f"vault_path should use JWT tenantX, got {vp}"
    assert "evil-tenant" not in vp


def test_safe_file_handling_rejects_traversal_and_oversize(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault-safe"
    monkeypatch.setenv("OAOS_WIKI_VAULT", str(vault_root))
    token = _make_wiki_jwt(scope="wiki:write")
    c = _client()
    r = c.post("/v1/personal-wiki/attachments", files={"file": ("../../etc/passwd", b"evil", "text/plain")}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code==403, r.text
    assert "PATH_TRAVERSAL" in r.text
    # Oversize 11MB should be 413
    big = b"x" * (11 * 1024 * 1024)
    r2 = c.post("/v1/personal-wiki/attachments", files={"file": ("big.bin", big, "application/octet-stream")}, headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code==413, r2.text
