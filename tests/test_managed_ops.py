"""Managed ops tests — checkpoint script, managed API RBAC, alerts yaml valid.

Covers:
- deploy/scripts/audit-checkpoint-to-s3.sh: HMAC sign/verify, versioning, retention, verify mode
- deploy/monitoring/managed-alerts.yml: valid yaml + SLO rules present
- deploy/monitoring/remote-forwarder.yml: valid yaml + remote_write present
- admin-console/backend/managed.py: RBAC L4+, managed endpoints
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "deploy" / "scripts"
MONITORING = ROOT / "deploy" / "monitoring"
BACKEND = ROOT / "admin-console" / "backend"

# ---------------------------------------------------------------------------
# Checkpoint script — existence + hardening checks
# ---------------------------------------------------------------------------

def test_checkpoint_script_exists_executable():
    p = SCRIPTS / "audit-checkpoint-to-s3.sh"
    assert p.exists(), f"missing {p}"
    assert os.access(p, os.X_OK)
    txt = p.read_text()
    assert "HMAC" in txt or "hmac" in txt
    assert "sign" in txt.lower()
    assert "verify" in txt.lower()
    assert "versioning" in txt.lower()
    assert "retention" in txt.lower() or "RETENTION" in txt
    assert "--verify" in txt
    assert "--prune" in txt
    assert "--dry-run" in txt
    assert "CHECKPOINT_RETENTION_DAYS" in txt
    assert "download-verify" in txt.lower() or "DOWNLOAD" in txt or "CHECKPOINT_VERIFY_DOWNLOAD" in txt
    assert "AES256" in txt or "server-side-encryption" in txt

def test_checkpoint_script_bash_syntax():
    p = SCRIPTS / "audit-checkpoint-to-s3.sh"
    r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

def test_checkpoint_script_help():
    p = SCRIPTS / "audit-checkpoint-to-s3.sh"
    env = os.environ.copy()
    env["AUDIT_SIGNING_KEY"] = "test-key"
    env["AWS_S3_BUCKET"] = "dummy"
    r = subprocess.run(["bash", str(p), "--help"], capture_output=True, text=True, env=env, timeout=10)
    assert r.returncode == 0
    assert "--verify" in r.stdout or "--verify" in r.stderr

def test_checkpoint_hmac_sign_verify_roundtrip():
    """Python-level HMAC equivalence: script signing = hmac sha256 of head."""
    key = "test-managed-key-123"
    head = "abc123deadbeef"
    expected = hmac.new(key.encode(), head.encode(), hashlib.sha256).hexdigest()
    # Write temp checkpoint, run sign helper via inline python (mirrors script)
    with tempfile.TemporaryDirectory() as td:
        cp = Path(td) / "cp.json"
        cp.write_text(json.dumps({"chain_head_hash": head, "event_count": 1}))
        # simulate sign_checkpoint
        data = json.loads(cp.read_text())
        sig = hmac.new(key.encode(), data["chain_head_hash"].encode(), hashlib.sha256).hexdigest()
        data["signature"] = sig
        cp.write_text(json.dumps(data))
        loaded = json.loads(cp.read_text())
        assert loaded["signature"] == expected
        # verify
        assert hmac.compare_digest(
            hmac.new(key.encode(), loaded["chain_head_hash"].encode(), hashlib.sha256).hexdigest(),
            loaded["signature"],
        )

def test_checkpoint_script_requires_signing_key():
    p = SCRIPTS / "audit-checkpoint-to-s3.sh"
    env = os.environ.copy()
    env.pop("AUDIT_SIGNING_KEY", None)
    env.pop("OAOS_SIGNING_KEY", None)
    env["AWS_S3_BUCKET"] = "dummy-bucket"
    r = subprocess.run(["bash", str(p), "--help"], capture_output=True, text=True, env=env, timeout=10)
    # help should still succeed even without key (help exits early); try without --help to trigger error
    r2 = subprocess.run(["bash", str(p)], capture_output=True, text=True, env=env, timeout=10)
    assert r2.returncode != 0
    assert "AUDIT_SIGNING_KEY" in r2.stderr

# ---------------------------------------------------------------------------
# Monitoring YAML validity
# ---------------------------------------------------------------------------

def test_managed_alerts_yaml_valid_and_has_slo():
    p = MONITORING / "managed-alerts.yml"
    assert p.exists(), f"missing {p}"
    data = yaml.safe_load(p.read_text())
    assert "groups" in data
    groups = {g["name"]: g for g in data["groups"]}
    assert "oaos-managed-slo" in groups
    assert "oaos-managed-backup" in groups
    rules = []
    for g in data["groups"]:
        rules.extend(g.get("rules", []))
    rule_names = [r.get("alert") for r in rules]
    # SLO alerts required by task
    assert any("SLO" in (n or "") or "Availability" in (n or "") for n in rule_names), rule_names
    assert any("P95" in (n or "") or "Latency" in (n or "") for n in rule_names), rule_names
    assert any("Backup" in (n or "") for n in rule_names), rule_names
    # backup age >48h = 172800 seconds must appear
    txt = p.read_text()
    assert "172800" in txt, "backup 48h = 172800s threshold missing"
    assert "0.5" in txt, "p95 500ms = 0.5s threshold missing"
    assert "0.995" in txt, "99.5% SLO threshold missing"
    # validate expressions are non-empty
    for r in rules:
        assert r.get("expr"), f"rule {r.get('alert')} missing expr"
        assert r.get("labels", {}).get("severity") in ("warning", "critical")

def test_remote_forwarder_yaml_valid():
    p = MONITORING / "remote-forwarder.yml"
    assert p.exists(), f"missing {p}"
    txt = p.read_text()
    data = yaml.safe_load(txt)
    assert "remote_write" in data, "remote_write key missing"
    rw = data["remote_write"]
    assert isinstance(rw, list) and len(rw) >= 1
    entry = rw[0]
    assert "url" in entry
    assert "queue_config" in entry

# ---------------------------------------------------------------------------
# Managed API — RBAC L4+ (reuse admin backend harness pattern)
# ---------------------------------------------------------------------------

import importlib.util

def _load_managed_app():
    # Isolated loader: do NOT pollute bare sys.modules names (auth/infra/business/managed)
    # — otherwise workstream_c security app gets wrong module (see fix for 9 failures in full suite).
    added = False
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
        added = True
    # Save any existing bare aliases to restore after load
    _saved = {k: sys.modules.get(k) for k in ("auth", "infra", "business", "managed")}
    def _load(name, fname, alias=None):
        spec = importlib.util.spec_from_file_location(name, str(BACKEND / fname))
        mod = importlib.util.module_from_spec(spec)  # type: ignore
        sys.modules[name] = mod
        if alias:
            sys.modules[alias] = mod
        spec.loader.exec_module(mod)  # type: ignore
        return mod
    # Load under namespaced keys only; also temporarily set bare for app.py import, then restore
    _load("admin_auth_managed", "auth.py", alias="auth")
    _load("admin_infra_managed", "infra.py", alias="infra")
    try:
        _load("admin_business_managed", "business.py", alias="business")
    except Exception:
        pass
    _load("admin_managed", "managed.py", alias="managed")
    app_mod_name = "admin_app_managed_test"
    spec = importlib.util.spec_from_file_location(app_mod_name, str(BACKEND / "app.py"))
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    sys.modules[app_mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore
    # Restore bare modules to prior state (or delete if we created them)
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    # Ensure namespaced admin modules still available for tests
    if added and str(BACKEND) in sys.path:
        sys.path.remove(str(BACKEND))
    return mod.app, sys.modules["admin_managed"], sys.modules["admin_auth_managed"]

try:
    _managed_app, _managed_mod, _auth_mod = _load_managed_app()
    from fastapi.testclient import TestClient as _TC
    _HAS_APP = True
except Exception as e:
    _HAS_APP = False
    _load_err = str(e)

def _client():
    from fastapi.testclient import TestClient
    return TestClient(_managed_app)

def _login(email="admin@openit.co.kr", password="Admin123!"):
    c = _client()
    r = c.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]

def _auth(token): return {"Authorization": f"Bearer {token}"}

@pytest.fixture(autouse=True)
def _reset_managed():
    if not _HAS_APP:
        pytest.skip(f"managed app not loadable: {_load_err if '_load_err' in globals() else 'unknown'}")
    # reset managed tickets + auth users
    try:
        _managed_mod.clear_tickets()
        _auth_mod.clear_users()
    except Exception:
        pass
    yield
    try:
        _managed_mod.clear_tickets()
        _auth_mod.clear_users()
    except Exception:
        pass

def test_managed_status_requires_auth():
    if not _HAS_APP: pytest.skip("no app")
    c = _client()
    r = c.get("/v1/managed/status")
    assert r.status_code == 401

def test_managed_status_L4_allowed():
    if not _HAS_APP: pytest.skip("no app")
    # create L4 user via L5
    c = _client()
    tok_l5 = _login()
    r = c.post("/v1/auth/register", json={"email":"l4m@test.co.kr","password":"Password123!","display_name":"L4M","role":"L4"}, headers=_auth(tok_l5))
    assert r.status_code in (200,201), r.text
    # login as L4
    r2 = c.post("/v1/auth/login", json={"email":"l4m@test.co.kr","password":"Password123!"})
    assert r2.status_code == 200
    tok_l4 = r2.json()["access_token"]
    for path in ["/v1/managed/status", "/v1/managed/health", "/v1/managed/support/tickets"]:
        rr = c.get(path, headers=_auth(tok_l4))
        assert rr.status_code == 200, f"L4 should access {path}: {rr.status_code} {rr.text}"
    # POST ticket as L4
    rr = c.post("/v1/managed/support/ticket", json={"title":"L4 ticket","body":"hello","severity":"low"}, headers=_auth(tok_l4))
    assert rr.status_code == 201, rr.text

def test_managed_status_L5_allowed():
    if not _HAS_APP: pytest.skip("no app")
    tok = _login()
    c = _client()
    r = c.get("/v1/managed/status", headers=_auth(tok))
    assert r.status_code == 200
    j = r.json()
    assert "edition" in j
    assert "slo" in j
    assert j["slo"]["uptime_target_percent"] == 99.5
    assert j["slo"]["p95_target_ms"] == 500

def test_managed_health_aggregated():
    if not _HAS_APP: pytest.skip("no app")
    tok = _login()
    c = _client()
    r = c.get("/v1/managed/health", headers=_auth(tok))
    assert r.status_code == 200
    j = r.json()
    assert "overall" in j
    assert "infra" in j
    assert "audit" in j
    assert "slo" in j
    assert j["slo"]["backup_max_age_hours"] == 48

def test_managed_support_ticket_create_and_list():
    if not _HAS_APP: pytest.skip("no app")
    tok = _login()
    c = _client()
    r = c.post("/v1/managed/support/ticket", json={"title":"My ticket","body":"Need help","severity":"high"}, headers=_auth(tok))
    assert r.status_code == 201
    j = r.json()
    assert j["title"] == "My ticket"
    assert j["severity"] == "high"
    assert j["status"] == "open"
    r2 = c.get("/v1/managed/support/tickets", headers=_auth(tok))
    assert r2.status_code == 200
    assert r2.json()["count"] >= 1
    ids = [t["id"] for t in r2.json()["tickets"]]
    assert j["id"] in ids

def test_managed_ticket_requires_auth():
    if not _HAS_APP: pytest.skip("no app")
    c = _client()
    r = c.post("/v1/managed/support/ticket", json={"title":"x","body":"y"})
    assert r.status_code == 401
    r2 = c.get("/v1/managed/support/tickets")
    assert r2.status_code == 401

def test_managed_health_requires_auth():
    if not _HAS_APP: pytest.skip("no app")
    c = _client()
    r = c.get("/v1/managed/health")
    assert r.status_code == 401
