"""Tenant LLM Quota — 3 tests: within limit / daily exceeded 429 / per-minute exceeded 429."""
import sys, importlib.util
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"

def _load(name, fn):
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location(name, str(BACKEND / fn))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m

auth_mod = _load("admin_auth_quota", "auth.py")
llm_mod = _load("admin_llm_quota", "llm_providers.py")
app_mod = _load("admin_app_quota", "app.py")

@pytest.fixture(autouse=True)
def isolate():
    import os
    os.environ["OAOS_RUNTIME_MODE"] = "llm"
    os.environ["OAOS_VAULT_KEY"] = "test-vault-key-for-llm-quota-32bytes!!"
    _orig = {}
    for k in ("admin_llm_quota", "llm_providers", "admin_console.backend.llm_providers"):
        try:
            m = sys.modules.get(k)
            if m is not None and hasattr(m, "_check_hermes_mode_guard"):
                _orig[k] = m._check_hermes_mode_guard
                m._check_hermes_mode_guard = lambda: None
        except:
            pass
    # also patch llm_mod directly (covers private module)
    try:
        if hasattr(llm_mod, "_check_hermes_mode_guard") and "admin_llm_quota" not in _orig:
            _orig["admin_llm_quota"] = llm_mod._check_hermes_mode_guard
            llm_mod._check_hermes_mode_guard = lambda: None
    except:
        pass
    # ensure runtime_mode is llm for canonical
    for rm_name in ("runtime_mode", "admin_console.backend.runtime_mode"):
        try:
            rm = sys.modules.get(rm_name)
            if rm and hasattr(rm, "set_mode"):
                rm.set_mode(rm.RuntimeMode.llm)  # type: ignore
        except:
            pass
    try:
        auth_mod.clear_users()
    except: pass
    try:
        llm_mod.clear_providers()
    except: pass
    for canon in ("admin_console.backend.llm_providers", "llm_providers"):
        try:
            m = sys.modules.get(canon)
            if m and hasattr(m, "clear_providers"):
                m.clear_providers()
        except:
            pass
    try:
        llm_mod.clear_quotas()
    except: pass
    for canon in ("admin_console.backend.llm_providers", "llm_providers"):
        try:
            m = sys.modules.get(canon)
            if m and hasattr(m, "clear_quotas"):
                m.clear_quotas()
        except:
            pass
    try:
        for canon in ("admin_console.backend.llm_providers", "llm_providers", "admin_llm_quota"):
            m = sys.modules.get(canon) if canon != "admin_llm_quota" else llm_mod
            if m and hasattr(m, "_quota_store"):
                m._quota_store.clear()
                m._quota_window_counts.clear()
    except:
        pass
    # also clear runtime quota
    try:
        from agent_runtime.llm_runtime import _llm_quota_clear
        _llm_quota_clear()
    except: pass
    yield
    for k, orig in _orig.items():
        try:
            m = sys.modules.get(k) if k != "admin_llm_quota" else llm_mod
            if m:
                m._check_hermes_mode_guard = orig
        except:
            pass
    # also restore llm_mod if not in orig
    try:
        if hasattr(llm_mod, "_check_hermes_mode_guard") and "admin_llm_quota" not in _orig:
            pass
    except:
        pass
    try: llm_mod.clear_providers()
    except: pass
    for canon in ("admin_console.backend.llm_providers", "llm_providers"):
        try:
            m = sys.modules.get(canon)
            if m and hasattr(m, "clear_providers"):
                m.clear_providers()
        except:
            pass
    try: llm_mod.clear_quotas()
    except: pass
    for canon in ("admin_console.backend.llm_providers", "llm_providers"):
        try:
            m = sys.modules.get(canon)
            if m and hasattr(m, "clear_quotas"):
                m.clear_quotas()
        except:
            pass
    try:
        from agent_runtime.llm_runtime import _llm_quota_clear
        _llm_quota_clear()
    except: pass
    try: auth_mod.clear_users()
    except: pass

def _client():
    return TestClient(app_mod.app)

def _login():
    c=_client()
    r=c.post("/v1/auth/login", json={"email":"admin@openit.co.kr","password":"Admin123!"})
    assert r.status_code==200, r.text
    return r.json()["access_token"]

def test_within_limit():
    token=_login()
    c=_client()
    h={"Authorization":f"Bearer {token}"}
    r=c.post("/v1/llm/providers", json={"provider":"claude","apiKey":"sk-test-1234567890"}, headers=h)
    assert r.status_code==201, r.text
    pid=r.json()["id"]
    # first call within limit should succeed
    r2=c.post(f"/v1/llm/providers/{pid}/test", headers={**h, "X-Tenant-Id":"tenant-within"})
    assert r2.status_code==200, r2.text
    assert r2.json()["status"] in ("ok","failed")

def test_daily_exceeded_429():
    token=_login()
    c=_client()
    h={"Authorization":f"Bearer {token}"}
    r=c.post("/v1/llm/providers", json={"provider":"claude","apiKey":"sk-test-1234567890"}, headers=h)
    pid=r.json()["id"]
    # set quota daily_limit=1, used_today=1 => next call exceeds
    for mod in [llm_mod, __import__("sys").modules.get("llm_providers"), __import__("sys").modules.get("admin_console.backend.llm_providers")]:
        if mod is not None:
            mod._quota_store["tenant-daily"]={"daily_limit":1,"per_minute_limit":10,"used_today":1,"window_start": __import__("datetime").datetime.now(__import__("datetime").timezone.utc), "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)}
            mod._quota_window_counts["tenant-daily"]=0
    r2=c.post(f"/v1/llm/providers/{pid}/test", headers={**h, "X-Tenant-Id":"tenant-daily"})
    assert r2.status_code==429, r2.text
    assert r2.json()["detail"]["code"]=="QUOTA_EXCEEDED"

def test_per_minute_exceeded_429():
    token=_login()
    c=_client()
    h={"Authorization":f"Bearer {token}"}
    r=c.post("/v1/llm/providers", json={"provider":"claude","apiKey":"«redacted:sk-…»"}, headers=h)
    pid=r.json()["id"]
    # per_minute_limit=1, first call ok, second within minute should 429
    for mod in [llm_mod, __import__("sys").modules.get("llm_providers"), __import__("sys").modules.get("admin_console.backend.llm_providers")]:
        if mod is not None:
            mod._quota_store.pop("tenant-permin",None)
            mod._quota_window_counts.pop("tenant-permin",None)
    for mod in [llm_mod, __import__("sys").modules.get("llm_providers"), __import__("sys").modules.get("admin_console.backend.llm_providers")]:
        if mod is not None:
            mod._quota_store["tenant-permin"]={"daily_limit":100,"per_minute_limit":1,"used_today":0,"window_start": __import__("datetime").datetime.now(__import__("datetime").timezone.utc), "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)}
            mod._quota_window_counts["tenant-permin"]=0
    r2=c.post(f"/v1/llm/providers/{pid}/test", headers={**h, "X-Tenant-Id":"tenant-permin"})
    assert r2.status_code==200, r2.text
    r3=c.post(f"/v1/llm/providers/{pid}/test", headers={**h, "X-Tenant-Id":"tenant-permin"})
    assert r3.status_code==429, r3.text
    assert r3.json()["detail"]["code"]=="QUOTA_EXCEEDED"
