"""Fallback runtime ownership gate — OAOS LLM Fallback vs Hermes Runtime.

Commercial server: Hermes Runtime is authoritative. OAOS LLM Fallback must not be
presented or written as Hermes Runtime config when runtime_mode == hermes.
OAOS LLM Runtime capability is kept for deployments that use it (runtime_mode==llm).

Tests:
- hermes mode: GET/PUT/POST /v1/llm/fallback → 409 HERMES_MODE_NOOP
- hermes mode: HERMES_CONFIG_PATH is never written even if env set
- llm mode: GET/PUT works, persists to DB/env, no 409
- llm mode + OAOS_ALLOW_HERMES_CONFIG_WRITE=1: HERMES_CONFIG_PATH mirror allowed
- llm mode without allow: mirror blocked (deprecated)
"""
from __future__ import annotations

import json
import os
import sys
import pathlib
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"

def _load(name, filename, alias=None):
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location(name, str(BACKEND / filename))
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    sys.modules[name] = mod
    if alias:
        sys.modules[alias] = mod
    spec.loader.exec_module(mod)  # type: ignore
    return mod

# Load deps in order: auth first, then runtime_mode, then fallback, then app
auth_mod = _load("admin_auth_gate", "auth.py", alias="auth")
runtime_mod = _load("admin_runtime_gate", "runtime_mode.py", alias="runtime_mode")
# ensure canonical alias for fallback's lazy import
sys.modules["admin_console.backend.runtime_mode"] = runtime_mod
sys.modules["admin_console.backend.auth"] = auth_mod
fallback_mod = _load("admin_fallback_gate", "fallback.py", alias="fallback")
sys.modules["admin_console.backend.fallback"] = fallback_mod
# persistence needed for DB
try:
    _ = _load("admin_persistence_gate", "persistence.py", alias="persistence")
except Exception:
    pass
app_mod = _load("admin_app_gate", "app.py")
if str(BACKEND) in sys.path:
    sys.path.remove(str(BACKEND))
admin_app = app_mod.app


@pytest.fixture(autouse=True)
def _isolate():
    # reset to hermes (commercial default) before each test
    auth_mod.clear_users()
    if hasattr(fallback_mod, "clear_fallback_cache"):
        fallback_mod.clear_fallback_cache()
    # clear env hermes paths
    for k in ("HERMES_CONFIG_PATH", "OAOS_HERMES_CONFIG_PATH", "OAOS_ALLOW_HERMES_CONFIG_WRITE", "OAOS_ALLOW_HERMES_FALLBACK_WRITE"):
        os.environ.pop(k, None)
    # force hermes mode
    try:
        runtime_mod.set_mode(runtime_mod.RuntimeMode.hermes)
    except Exception:
        runtime_mod._current_mode = runtime_mod.RuntimeMode.hermes
        os.environ.pop("OAOS_RUNTIME_MODE", None)
    yield
    auth_mod.clear_users()
    if hasattr(fallback_mod, "clear_fallback_cache"):
        fallback_mod.clear_fallback_cache()
    for k in ("HERMES_CONFIG_PATH", "OAOS_HERMES_CONFIG_PATH", "OAOS_ALLOW_HERMES_CONFIG_WRITE", "OAOS_ALLOW_HERMES_FALLBACK_WRITE"):
        os.environ.pop(k, None)
    try:
        runtime_mod.set_mode(runtime_mod.RuntimeMode.hermes)
    except Exception:
        pass


def _client():
    return TestClient(admin_app)


def _login(email="admin@openit.co.kr", password="Admin123!"):
    c = _client()
    r = c.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def _set_llm():
    runtime_mod.set_mode(runtime_mod.RuntimeMode.llm)
    assert runtime_mod.get_mode() == runtime_mod.RuntimeMode.llm


def _set_hermes():
    runtime_mod.set_mode(runtime_mod.RuntimeMode.hermes)
    assert runtime_mod.get_mode() == runtime_mod.RuntimeMode.hermes


# ---- hermes mode: API is noop 409 ----

def test_hermes_get_fallback_409():
    _set_hermes()
    tok = _login()
    c = _client()
    r = c.get("/v1/llm/fallback", headers=_auth(tok))
    assert r.status_code == 409, r.text
    detail = r.json().get("detail", {})
    assert detail.get("code") == "HERMES_MODE_NOOP"
    assert "Hermes Runtime" in detail.get("message", "")


def test_hermes_put_fallback_409():
    _set_hermes()
    tok = _login()
    c = _client()
    r = c.put("/v1/llm/fallback", json={"enabled": True, "chain": [{"provider": "claude", "model": "m1", "enabled": True}]}, headers=_auth(tok))
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "HERMES_MODE_NOOP"


def test_hermes_post_fallback_409():
    _set_hermes()
    tok = _login()
    c = _client()
    r = c.post("/v1/llm/fallback", json={"enabled": True, "chain": []}, headers=_auth(tok))
    assert r.status_code == 409


def test_hermes_hermes_config_never_written():
    _set_hermes()
    # create temp json file for Hermes config
    tmp = pathlib.Path("/tmp/test_hermes_config_hermes_mode.json")
    tmp.write_text(json.dumps({"existing": 1}), encoding="utf-8")
    os.environ["HERMES_CONFIG_PATH"] = str(tmp)
    tok = _login()
    c = _client()
    # even PUT is blocked, so no write
    r = c.put("/v1/llm/fallback", json={"enabled": True, "chain": []}, headers=_auth(tok))
    assert r.status_code == 409
    # file unchanged
    data = json.loads(tmp.read_text(encoding="utf-8"))
    assert "fallback" not in data
    assert data["existing"] == 1
    # also direct _write_hermes_config is blocked
    fallback_mod._write_hermes_config(fallback_mod.FallbackConfig(enabled=True, chain=[], fallback_model=None))
    data2 = json.loads(tmp.read_text(encoding="utf-8"))
    assert "fallback" not in data2
    tmp.unlink(missing_ok=True)


# ---- llm mode: API works ----

def test_llm_get_put_works():
    _set_llm()
    tok = _login()
    c = _client()
    r = c.get("/v1/llm/fallback", headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert "enabled" in r.json()
    r2 = c.put("/v1/llm/fallback", json={"enabled": True, "chain": [{"provider": "claude", "model": "sonnet", "enabled": True}], "fallback_model": "qwen2.5"}, headers=_auth(tok))
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["enabled"] is True
    assert len(body["chain"]) == 1
    assert body["chain"][0]["provider"] == "claude"
    assert body["fallback_model"] == "qwen2.5"
    # GET again shows persisted
    r3 = c.get("/v1/llm/fallback", headers=_auth(tok))
    assert r3.status_code == 200
    assert r3.json()["fallback_model"] == "qwen2.5"


def test_llm_hermes_mirror_blocked_without_allow():
    _set_llm()
    tmp = pathlib.Path("/tmp/test_hermes_config_llm_no_allow.json")
    tmp.write_text(json.dumps({"existing": 1}), encoding="utf-8")
    os.environ["HERMES_CONFIG_PATH"] = str(tmp)
    # no allow flag → mirror blocked
    tok = _login()
    c = _client()
    r = c.put("/v1/llm/fallback", json={"enabled": True, "chain": [{"provider": "ollama", "enabled": True}]}, headers=_auth(tok))
    assert r.status_code == 200
    data = json.loads(tmp.read_text(encoding="utf-8"))
    assert "fallback" not in data, "without OAOS_ALLOW_HERMES_CONFIG_WRITE mirror must be blocked"
    tmp.unlink(missing_ok=True)


def test_llm_hermes_mirror_allowed_with_optin():
    _set_llm()
    tmp = pathlib.Path("/tmp/test_hermes_config_llm_allow.json")
    tmp.write_text(json.dumps({"existing": 1}), encoding="utf-8")
    os.environ["HERMES_CONFIG_PATH"] = str(tmp)
    os.environ["OAOS_ALLOW_HERMES_CONFIG_WRITE"] = "1"
    tok = _login()
    c = _client()
    r = c.put("/v1/llm/fallback", json={"enabled": False, "chain": [{"provider": "gemini", "model": "1.5-pro", "enabled": True}], "fallback_model": "mX"}, headers=_auth(tok))
    assert r.status_code == 200
    data = json.loads(tmp.read_text(encoding="utf-8"))
    assert "fallback" in data
    assert data["fallback"]["enabled"] is False
    assert data["fallback"]["chain"][0]["provider"] == "gemini"
    assert data["existing"] == 1
    tmp.unlink(missing_ok=True)


def test_llm_fallback_validation_still_enforced():
    _set_llm()
    tok = _login()
    c = _client()
    # chain too long (21)
    long_chain = [{"provider": "claude", "enabled": True} for _ in range(21)]
    r = c.put("/v1/llm/fallback", json={"chain": long_chain}, headers=_auth(tok))
    assert r.status_code == 400
    # invalid provider
    r2 = c.put("/v1/llm/fallback", json={"chain": [{"provider": "bad-provider", "enabled": True}]}, headers=_auth(tok))
    assert r2.status_code == 422  # pydantic validation


def test_is_hermes_owned_helper():
    _set_hermes()
    assert fallback_mod._is_hermes_owned() is True
    assert fallback_mod._is_hermes_config_mirror_allowed() is False
    _set_llm()
    assert fallback_mod._is_hermes_owned() is False
    assert fallback_mod._is_hermes_config_mirror_allowed() is False
    os.environ["OAOS_ALLOW_HERMES_CONFIG_WRITE"] = "1"
    assert fallback_mod._is_hermes_config_mirror_allowed() is True
    _set_hermes()
    assert fallback_mod._is_hermes_config_mirror_allowed() is False  # hermes always blocks even with allow
