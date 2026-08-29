"""tests/test_llm_usage.py — LLM cost/latency dashboard (011)."""
import sys, importlib.util
from pathlib import Path
import os, asyncio
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"
RUNTIME_FILE = ROOT / "packages" / "agent-runtime" / "agent_runtime" / "llm_runtime.py"
for p in [str(ROOT / "admin-console"), str(BACKEND)]:
    if p not in sys.path:
        sys.path.insert(0, p)

def _load(name, fn):
    spec = importlib.util.spec_from_file_location(name, str(BACKEND / fn))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m

auth_mod = _load("admin_auth_usage", "auth.py")
llm_mod = _load("admin_llm_usage", "llm_providers.py")
app_mod = _load("admin_app_usage", "app.py")

from agent_runtime.llm_runtime import (
    LLMProviderAdapter,
    clear_llm_usage,
    get_llm_usage_history,
    get_llm_usage_summary,
)

@pytest.fixture(autouse=True)
def isolate():
    os.environ["OAOS_RUNTIME_MODE"] = "llm"
    os.environ["OAOS_VAULT_KEY"] = "test-vault-key-32b-test-vault-key!!"
    _orig = {}
    for k in ("admin_llm_usage", "llm_providers", "admin_console.backend.llm_providers"):
        try:
            m = sys.modules.get(k)
            if m is not None and hasattr(m, "_check_hermes_mode_guard"):
                _orig[k] = m._check_hermes_mode_guard  # type: ignore
                m._check_hermes_mode_guard = lambda: None  # type: ignore
        except: pass
    try:
        if hasattr(llm_mod, "_check_hermes_mode_guard") and "admin_llm_usage" not in _orig:
            _orig["admin_llm_usage"] = llm_mod._check_hermes_mode_guard  # type: ignore
            llm_mod._check_hermes_mode_guard = lambda: None  # type: ignore
    except: pass
    for rm_name in ("runtime_mode", "admin_console.backend.runtime_mode"):
        try:
            rm = sys.modules.get(rm_name)
            if rm and hasattr(rm, "set_mode"):
                rm.set_mode(rm.RuntimeMode.llm)  # type: ignore
        except: pass
    try: auth_mod.clear_users()
    except: pass
    try: llm_mod.clear_providers()
    except: pass
    for canon in ("admin_console.backend.llm_providers", "llm_providers"):
        try:
            m = sys.modules.get(canon)
            if m and hasattr(m, "clear_providers"):
                m.clear_providers()
        except: pass
    try: llm_mod.clear_quotas()
    except: pass
    for canon in ("admin_console.backend.llm_providers", "llm_providers"):
        try:
            m = sys.modules.get(canon)
            if m and hasattr(m, "clear_quotas"):
                m.clear_quotas()
        except: pass
    try: llm_mod.clear_usage()
    except: pass
    for canon in ("admin_console.backend.llm_providers", "llm_providers"):
        try:
            m = sys.modules.get(canon)
            if m and hasattr(m, "clear_usage"):
                m.clear_usage()
        except: pass
    try: llm_mod._admin_usage_records.clear()
    except: pass
    for canon in ("admin_console.backend.llm_providers", "llm_providers"):
        try:
            m = sys.modules.get(canon)
            if m and hasattr(m, "_admin_usage_records"):
                m._admin_usage_records.clear()  # type: ignore
        except: pass
    clear_llm_usage()
    yield
    for k, orig in _orig.items():
        try:
            m = sys.modules.get(k) if k != "admin_llm_usage" else llm_mod
            if m:
                m._check_hermes_mode_guard = orig  # type: ignore
        except: pass
    try: llm_mod.clear_providers()
    except: pass
    for canon in ("admin_console.backend.llm_providers", "llm_providers"):
        try:
            m = sys.modules.get(canon)
            if m and hasattr(m, "clear_providers"):
                m.clear_providers()
        except: pass
    try: llm_mod.clear_quotas()
    except: pass
    for canon in ("admin_console.backend.llm_providers", "llm_providers"):
        try:
            m = sys.modules.get(canon)
            if m and hasattr(m, "clear_quotas"):
                m.clear_quotas()
        except: pass
    try: llm_mod.clear_usage()
    except: pass
    for canon in ("admin_console.backend.llm_providers", "llm_providers"):
        try:
            m = sys.modules.get(canon)
            if m and hasattr(m, "clear_usage"):
                m.clear_usage()
        except: pass
    try: llm_mod._admin_usage_records.clear()
    except: pass
    clear_llm_usage()
    try: auth_mod.clear_users()
    except: pass

def _client():
    return TestClient(app_mod.app)

def _login():
    c=_client()
    r=c.post("/v1/auth/login", json={"email":"admin@openit.co.kr","password":"Admin123!"})
    assert r.status_code==200, r.text
    return r.json()["access_token"]

def test_usage_recorded_on_success():
    token=_login()
    c=_client()
    h={"Authorization": f"Bearer {token}"}
    r=c.post("/v1/llm/providers", json={"provider":"claude","apiKey":"sk-test-1234567890","name":"t"}, headers=h)
    assert r.status_code==201, r.text
    pid=r.json()["id"]
    r2=c.post(f"/v1/llm/providers/{pid}/test", headers={**h, "X-Tenant-Id":"tenant-usage-1"})
    assert r2.status_code==200, r2.text
    rh=c.get("/v1/llm/usage/history?limit=10", headers=h)
    assert rh.status_code==200, rh.text
    j=rh.json()
    assert j["count"] >= 1
    assert any(x["tenant_id"]=="tenant-usage-1" for x in j["items"])
    rs=c.get("/v1/llm/usage/summary?tenant_id=tenant-usage-1", headers=h)
    assert rs.status_code==200, rs.text
    s=rs.json()
    assert s["total_requests"] >= 1
    assert s["daily_count"] >= 1
    assert s["per_minute_count"] >= 1
    assert "total_cost_usd" in s
    assert "avg_latency_ms" in s
    assert "p95_latency_ms" in s

def test_usage_recorded_on_fail_and_quota_linked():
    token=_login()
    c=_client()
    h={"Authorization": f"Bearer {token}"}
    r=c.post("/v1/llm/providers", json={"provider":"claude","apiKey":"«redacted:sk-…»"}, headers=h)
    pid=r.json()["id"]
    for mod in [llm_mod, sys.modules.get("llm_providers"), sys.modules.get("admin_console.backend.llm_providers")]:
        if mod is not None:
            mod._quota_store["tenant-fail"]={"daily_limit":1,"per_minute_limit":10,"used_today":1,"window_start": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)}
            mod._quota_window_counts["tenant-fail"]=0
    r2=c.post(f"/v1/llm/providers/{pid}/test", headers={**h, "X-Tenant-Id":"tenant-fail"})
    assert r2.status_code==429, r2.text
    rh=c.get("/v1/llm/usage/history?limit=10&tenant_id=tenant-fail", headers=h)
    assert rh.status_code==200, rh.text
    assert rh.json()["count"] >= 1
    assert any(x["status"]=="failed" for x in rh.json()["items"])
    rs=c.get("/v1/llm/usage/summary?tenant_id=tenant-fail", headers=h)
    assert rs.json()["failed_count"] >= 1

def test_runtime_latency_token_cost_tracking():
    os.environ["OAOS_MOCK_FALLBACK"]="1"
    clear_llm_usage()
    adapter=LLMProviderAdapter(model="gpt-4o-mini", mock_responses=[{"choices":[{"message":{"content":"hello"}}], "usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}])
    async def _run():
        return await adapter._raw_completion(messages=[{"role":"user","content":"hi"}], model="gpt-4o-mini", trace_id="t1", request_id="r1", oaos_context=None)
    asyncio.run(_run())
    hist=get_llm_usage_history(limit=5)
    assert len(hist) >= 1
    rec=hist[0]
    assert rec["prompt_tokens"]==10
    assert rec["completion_tokens"]==20
    assert rec["total_tokens"]==30
    assert rec["cost_usd"] > 0
    assert rec["latency_ms"] >= 0
    summ=get_llm_usage_summary()
    assert summ["total_requests"] >= 1
    assert summ["total_cost_usd"] > 0

def test_tenant_isolation():
    token=_login()
    c=_client()
    h={"Authorization": f"Bearer {token}"}
    r=c.post("/v1/llm/providers", json={"provider":"claude","apiKey":"sk-abc-1234567890"}, headers=h)
    pid=r.json()["id"]
    c.post(f"/v1/llm/providers/{pid}/test", headers={**h, "X-Tenant-Id":"tenant-A"})
    c.post(f"/v1/llm/providers/{pid}/test", headers={**h, "X-Tenant-Id":"tenant-B"})
    rs_a=c.get("/v1/llm/usage/summary?tenant_id=tenant-A", headers=h).json()
    rs_b=c.get("/v1/llm/usage/summary?tenant_id=tenant-B", headers=h).json()
    assert rs_a["total_requests"] >= 1
    assert rs_b["total_requests"] >= 1
    ha=c.get("/v1/llm/usage/history?tenant_id=tenant-A&limit=5", headers=h).json()
    hb=c.get("/v1/llm/usage/history?tenant_id=tenant-B&limit=5", headers=h).json()
    assert all(x["tenant_id"]=="tenant-A" for x in ha["items"])
    assert all(x["tenant_id"]=="tenant-B" for x in hb["items"])
