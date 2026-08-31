"""Regression: clear_runtime_config must never wipe non-sqlite or production DB.

Verifies defense-in-depth guards added 2026-08-31:
- production OAOS_ENV blocks DB wipe even with OAOS_ALLOW_DESTRUCTIVE flag
- non-sqlite postgres URLs blocked in non-prod
- sqlite isolated URLs allowed to clear
- teardown leak scenario: call clear after env restored to production must not wipe

Does NOT write production DB; uses mocks for engine.
"""
from __future__ import annotations
import os, sys, pathlib, importlib, types, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"
CP_ROOT = ROOT / "control-plane"
for p in [str(CP_ROOT), str(BACKEND), str(ROOT / "security" / "policy-engine")]:
    if p not in sys.path:
        sys.path.insert(0, p)

def _load_admin_rc():
    for pkg in ("admin_console","admin_console.backend"):
        if pkg not in sys.modules:
            m=types.ModuleType(pkg); m.__path__=[]; sys.modules[pkg]=m
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location("admin_console.backend.runtime_config", str(BACKEND/"runtime_config.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["admin_console.backend.runtime_config"]=mod
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod

def _load_cp_rc():
    if str(CP_ROOT) not in sys.path:
        sys.path.insert(0, str(CP_ROOT))
    import importlib.util as iu
    spec = iu.spec_from_file_location("control_plane.runtime_config", str(CP_ROOT/"control_plane"/"runtime_config.py"))
    mod = iu.module_from_spec(spec)
    sys.modules["control_plane.runtime_config"]=mod
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod

def _set_env(**kw):
    old={}
    for k,v in kw.items():
        old[k]=os.environ.get(k)
        if v is None:
            os.environ.pop(k,None)
        else:
            os.environ[k]=v
    return old
def _restore(old):
    for k,v in old.items():
        if v is None:
            os.environ.pop(k,None)
        else:
            os.environ[k]=v

def test_admin_guard_blocks_production_postgres_even_with_allow_flag(monkeypatch):
    rc=_load_admin_rc()
    # fake engine that would track deletes
    deletes=[]
    class FakeConn:
        def __enter__(self): return self
        def __exit__(self,*_): return False
        def execute(self,*a,**kw): deletes.append((a,kw)); return None
    class FakeEng:
        def begin(self):
            class Ctx:
                def __enter__(self): return FakeConn()
                def __exit__(self,*_): return False
            return Ctx()
        def dispose(self): pass
    monkeypatch.setattr(rc, "_db_engine", lambda: FakeEng())
    monkeypatch.setattr(rc, "_ensure_runtime_tables_sync", lambda e: None)
    old=_set_env(OAOS_ENV="production", DATABASE_URL="postgresql+asyncpg://oaos:secret@localhost:5432/oaos",
                 OAOS_DATABASE_URL="postgresql+asyncpg://oaos:secret@localhost:5432/oaos",
                 OAOS_ALLOW_DESTRUCTIVE_RUNTIME_CONFIG_CLEAR="1")
    try:
        assert rc._is_destructive_db_allowed() is False, "production must block even with allow flag"
        rc.clear_runtime_config()
        assert deletes==[], "must not have executed DELETE on production postgres"
    finally:
        _restore(old)

def test_admin_guard_blocks_non_sqlite_nonprod(monkeypatch):
    rc=_load_admin_rc()
    deletes=[]
    class FakeConn:
        def __enter__(self): return self
        def __exit__(self,*_): return False
        def execute(self,*a,**kw): deletes.append(1); return None
    class FakeEng:
        def begin(self):
            class Ctx:
                def __enter__(self): return FakeConn()
                def __exit__(self,*_): return False
            return Ctx()
        def dispose(self): pass
    monkeypatch.setattr(rc, "_db_engine", lambda: FakeEng())
    monkeypatch.setattr(rc, "_ensure_runtime_tables_sync", lambda e: None)
    old=_set_env(OAOS_ENV="development", DATABASE_URL="postgresql+asyncpg://oaos:secret@localhost:5432/oaos",
                 OAOS_DATABASE_URL="postgresql+asyncpg://oaos:secret@localhost:5432/oaos",
                 OAOS_ALLOW_DESTRUCTIVE_RUNTIME_CONFIG_CLEAR=None)
    # ensure flag not set
    os.environ.pop("OAOS_ALLOW_DESTRUCTIVE_RUNTIME_CONFIG_CLEAR",None)
    try:
        assert rc._is_destructive_db_allowed() is False
        rc.clear_runtime_config()
        assert deletes==[]
    finally:
        _restore(old)

def test_admin_guard_allows_sqlite(monkeypatch, tmp_path):
    rc=_load_admin_rc()
    # use real sqlite file to verify wipe happens
    db_file = tmp_path / "guard_test.db"
    url = f"sqlite:///{db_file}"
    old=_set_env(OAOS_ENV="development", DATABASE_URL=url, OAOS_DATABASE_URL=url)
    os.environ.pop("OAOS_ALLOW_DESTRUCTIVE_RUNTIME_CONFIG_CLEAR",None)
    try:
        assert rc._is_destructive_db_allowed() is True
        # create dummy table via engine then clear
        from sqlalchemy import create_engine, text
        eng=create_engine(url)
        with eng.begin() as c:
            c.execute(text("CREATE TABLE IF NOT EXISTS admin_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT, updated_by TEXT)"))
            c.execute(text("INSERT OR REPLACE INTO admin_settings (key,value,updated_at,updated_by) VALUES ('runtime_config:snapshot:default:1','{}','now','x')"))
        eng.dispose()
        rc.clear_runtime_config()
        # verify deleted
        eng2=create_engine(url)
        with eng2.connect() as c:
            row=c.execute(text("SELECT count(*) FROM admin_settings WHERE key LIKE 'runtime_config:%'")).fetchone()
            assert row[0]==0
        eng2.dispose()
    finally:
        _restore(old)

def test_admin_guard_allows_sqlite_even_in_production_host_should_block():
    """Even on production host, sqlite is considered isolated? Current policy: production blocks all.
    So sqlite in production should still be blocked (defense-in-depth prefers block)."""
    rc=_load_admin_rc()
    old=_set_env(OAOS_ENV="production", DATABASE_URL="sqlite:////tmp/test.db", OAOS_DATABASE_URL="sqlite:////tmp/test.db")
    try:
        # per new strict guard, production blocks even sqlite — intentional fail-closed
        assert rc._is_destructive_db_allowed() is False
    finally:
        _restore(old)

def test_cp_guard_blocks_production_and_allows_sqlite(monkeypatch, tmp_path):
    cprc=_load_cp_rc()
    deletes=[]
    class FakeConn:
        def __enter__(self): return self
        def __exit__(self,*_): return False
        def execute(self,*a,**kw): deletes.append(1); return None
    class FakeEng:
        def begin(self):
            class Ctx:
                def __enter__(self): return FakeConn()
                def __exit__(self,*_): return False
            return Ctx()
        def dispose(self): pass
    # production postgres must be blocked
    old=_set_env(OAOS_ENV="production", DATABASE_URL="postgresql://oaos:secret@localhost:5432/oaos", OAOS_DATABASE_URL="postgresql://oaos:secret@localhost:5432/oaos")
    orig_db_url = cprc._db_url
    try:
        # patch _db_url to return postgres
        assert cprc._is_destructive_allowed() is False
        # mock engine
        monkeypatch.setattr(cprc, "_db_url", lambda: "postgresql://oaos:secret@localhost:5432/oaos")
        monkeypatch.setattr(cprc, "_ensure_runtime_tables_sync", lambda e: None)
        from unittest.mock import patch
        with patch("sqlalchemy.create_engine", return_value=FakeEng()):
            cprc.clear_runtime_config_state()
        assert deletes==[]
    finally:
        _restore(old)
        # restore original _db_url for next check
        try:
            monkeypatch.setattr(cprc, "_db_url", orig_db_url)
        except Exception:
            cprc._db_url = orig_db_url  # type: ignore
    # sqlite dev allowed
    dbf = tmp_path / "cp_guard.db"
    url=f"sqlite:///{dbf}"
    old2=_set_env(OAOS_ENV="development", DATABASE_URL=url, OAOS_DATABASE_URL=url)
    os.environ.pop("OAOS_ALLOW_DESTRUCTIVE_RUNTIME_CONFIG_CLEAR",None)
    try:
        assert cprc._is_destructive_allowed() is True
    finally:
        _restore(old2)

def test_teardown_leak_simulation_no_wipe(monkeypatch):
    """Simulate leak: test sets sqlite, yields, then teardown restores production and calls clear.
    Guard must prevent wipe."""
    rc=_load_admin_rc()
    cprc=_load_cp_rc()
    # Simulate test body with sqlite
    old_prod = _set_env(OAOS_ENV="development", DATABASE_URL="sqlite:////tmp/isolated.db", OAOS_DATABASE_URL="sqlite:////tmp/isolated.db")
    # ...test does work...
    # Now teardown restores production (leak)
    _restore(old_prod)
    # Now production env active (from real env)
    # Ensure guard would block
    # Use monkeypatch to fake engine that would track
    deletes=[]
    class FakeConn:
        def __enter__(self): return self
        def __exit__(self,*_): return False
        def execute(self,*a,**kw): deletes.append(1); return None
    class FakeEng:
        def begin(self):
            class Ctx:
                def __enter__(self): return FakeConn()
                def __exit__(self,*_): return False
            return Ctx()
        def dispose(self): pass
    monkeypatch.setattr(rc, "_db_engine", lambda: FakeEng())
    monkeypatch.setattr(rc, "_ensure_runtime_tables_sync", lambda e: None)
    # force production url
    old2=_set_env(OAOS_ENV="production", DATABASE_URL="postgresql+asyncpg://oaos:secret@localhost:5432/oaos", OAOS_DATABASE_URL="postgresql+asyncpg://oaos:secret@localhost:5432/oaos")
    try:
        rc.clear_runtime_config()
        cprc.clear_runtime_config_state()
        assert deletes==[]
    finally:
        _restore(old2)
