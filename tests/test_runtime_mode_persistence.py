"""Test runtime_mode DB persistence (H-2)."""
from __future__ import annotations
import os
import sys
sys.path.insert(0, "admin-console")
import tempfile
import pytest

def test_runtime_mode_db_persistence_sqlite(monkeypatch):
    """set_mode() persists to admin_settings, get_mode() reads back."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("OAOS_DATABASE_URL", db_url)
    # Reset engine singleton
    import backend.runtime_mode as rm
    rm._db_engine = None
    rm._current_mode = rm.RuntimeMode.hermes
    # Set to llm
    rm.set_mode(rm.RuntimeMode.llm)
    assert os.environ.get("OAOS_RUNTIME_MODE") == "llm"
    # Verify DB
    persisted = rm._db_get_mode()
    assert persisted == "llm", f"DB should have llm, got {persisted}"
    # Simulate restart: reset in-memory to hermes, then get_mode should read DB
    rm._current_mode = rm.RuntimeMode.hermes
    os.environ["OAOS_RUNTIME_MODE"] = "hermes"
    got = rm.get_mode()
    assert got == rm.RuntimeMode.llm, f"get_mode should return llm from DB, got {got}"
    # Set back to hermes
    rm.set_mode(rm.RuntimeMode.hermes)
    assert rm._db_get_mode() == "hermes"
    # Cleanup
    rm._db_engine = None
    try:
        os.unlink(db_path)
    except Exception:
        pass
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("OAOS_DATABASE_URL", None)
    rm._current_mode = rm.RuntimeMode.hermes
    rm._db_engine = None

def test_runtime_mode_fallback_without_db(monkeypatch):
    """Without DATABASE_URL, fallback to env/in-memory must work."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    import backend.runtime_mode as rm
    rm._db_engine = None
    rm._current_mode = rm.RuntimeMode.hermes
    assert rm.get_mode() == rm.RuntimeMode.hermes
    rm.set_mode(rm.RuntimeMode.llm)
    assert rm.get_mode() == rm.RuntimeMode.llm
    # No DB, so _db_get_mode returns None but in-memory still works
    assert rm._db_get_mode() is None
    rm._current_mode = rm.RuntimeMode.hermes
    rm._db_engine = None
