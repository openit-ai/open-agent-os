"""Test embed stub — no hard deps, hash fallback, vault wiring."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _ensure_personal_wiki_package():
    """Ensure real packages/personal-wiki package is importable (admin file shadows it when backend is on path)."""
    root = Path(__file__).resolve().parents[1]
    pkg = str(root / "packages" / "personal-wiki")
    # force pkg to front so find_spec prefers package over admin-console/backend/personal_wiki.py
    if pkg in sys.path:
        sys.path.remove(pkg)
    sys.path.insert(0, pkg)
    # if personal_wiki currently points to admin file (no __path__), restore package
    mod = sys.modules.get("personal_wiki")
    if mod is not None and not hasattr(mod, "__path__"):
        # keep admin alias
        if "admin_personal_wiki" not in sys.modules:
            sys.modules["admin_personal_wiki"] = mod
        del sys.modules["personal_wiki"]
        for k in list(sys.modules.keys()):
            if k.startswith("personal_wiki."):
                del sys.modules[k]
        # reimport package
        try:
            import importlib
            importlib.import_module("personal_wiki")
        except Exception:
            pass


def test_embed_stub_hash_and_chunk_and_file():
    _ensure_personal_wiki_package()
    from personal_wiki.embed import chunk_text, hash_embedding, get_embedding, embed_file_sync, is_embed_enabled
    from personal_wiki.vault import append_journal, upsert_note

    # hash is deterministic & normalized
    v1 = hash_embedding("hello world", dim=1536)
    v2 = hash_embedding("hello world", dim=1536)
    assert len(v1) == 1536
    assert v1 == v2
    v3 = hash_embedding("different", dim=1536)
    assert v1 != v3
    import math

    norm = math.sqrt(sum(x * x for x in v1))
    assert abs(norm - 1.0) < 1e-6

    # chunking
    text = "a" * 2000
    chunks = chunk_text(text, chunk_size=800, overlap=100)
    assert len(chunks) >= 3
    assert all(len(c) <= 800 for c in chunks)
    assert chunk_text("", chunk_size=800) == []
    assert chunk_text("short", chunk_size=800) == ["short"]

    # get_embedding fallback hash when no key/service
    os.environ.pop("OAOS_EMBED_API_URL", None)
    os.environ.pop("OAOS_EMBED_API_KEY", None)
    os.environ.pop("OPENAI_API_KEY", None)
    emb = get_embedding("test embed", dim=1536)
    assert len(emb) == 1536
    assert abs(math.sqrt(sum(x * x for x in emb)) - 1.0) < 1e-6

    # embed_file_sync reads vault file path, chunks, returns mock when no DB
    orig_db = os.environ.pop("DATABASE_URL", None)
    orig_oaos = os.environ.pop("OAOS_DATABASE_URL", None)
    orig_svc = os.environ.pop("OAOS_MEMORY_SERVICE_URL", None)
    try:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td) / "vault"
            note_path = upsert_note("test/slug", "hello personal wiki content " * 50, vault_root=vault_root)
            assert note_path is not None and note_path.exists()
            res = embed_file_sync(note_path, metadata={"kind": "note", "slug": "test/slug"})
            assert res["chunks"] >= 1
            assert res["mock"] is True
            assert "ids" in res

            os.environ["OAOS_EMBED_ENABLED"] = "1"
            assert is_embed_enabled() is True
            jfile = append_journal("trace-123", "web_search", {"result": "hello"}, vault_root=vault_root)
            assert jfile is not None and jfile.exists()
            os.environ["OAOS_EMBED_ENABLED"] = "0"
            assert is_embed_enabled() is False
            jfile2 = append_journal("trace-124", "tool2", "world", vault_root=vault_root)
            assert jfile2 is not None
    finally:
        if orig_db is not None:
            os.environ["DATABASE_URL"] = orig_db
        if orig_oaos is not None:
            os.environ["OAOS_DATABASE_URL"] = orig_oaos
        if orig_svc is not None:
            os.environ["OAOS_MEMORY_SERVICE_URL"] = orig_svc
        os.environ["OAOS_EMBED_ENABLED"] = "0"
