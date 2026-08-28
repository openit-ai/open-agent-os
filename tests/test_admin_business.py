"""Business edition Admin Console tests — license verify RBAC, backup trigger, security updates.

Covers: admin-console/backend/business.py (BSL 1.1 §41, §16A.3.1, §22 RBAC)
- POST /v1/license/verify — production license check
- GET /v1/license/status, /v1/security/updates, /v1/backup/status, POST /v1/backup/trigger, GET /v1/upgrade/status
"""

from __future__ import annotations

import importlib.util
import sys
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


auth_mod = _load_admin_module("admin_auth_biz", "auth.py", bare_alias="auth")
# infra needed for app import (app imports infra router)
infra_mod = _load_admin_module("admin_infra_biz", "infra.py", bare_alias="infra")
business_mod = _load_admin_module("admin_business", "business.py", bare_alias="business")
_app_mod = _load_admin_module("admin_app_biz", "app.py")
if str(BACKEND) in sys.path:
    sys.path.remove(str(BACKEND))

admin_app = _app_mod.app


@pytest.fixture(autouse=True)
def isolate_business():
    auth_mod.clear_users()
    infra_mod.clear_services()
    business_mod.clear_license()
    business_mod.clear_backups()
    business_mod.clear_upgrade()
    yield
    business_mod.clear_license()
    business_mod.clear_backups()
    business_mod.clear_upgrade()
    auth_mod.clear_users()
    infra_mod.clear_services()


def _client():
    return TestClient(admin_app)


def _login(email="admin@openit.co.kr", password="Admin123!"):
    c = _client()
    r = c.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _make_l4():
    token_l5 = _login()
    c = _client()
    # register L4
    c.post(
        "/v1/auth/register",
        json={"email": "viewer@test.co.kr", "password": "Password123!", "display_name": "Viewer", "role": "L4"},
        headers=_auth(token_l5),
    )
    r = c.post("/v1/auth/login", json={"email": "viewer@test.co.kr", "password": "Password123!"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"], token_l5


# ---------------------------------------------------------------------------
# License verify RBAC + validation
# ---------------------------------------------------------------------------

def test_license_verify_requires_auth():
    c = _client()
    r = c.post("/v1/license/verify", json={"license_key": "OPENIT-BUSINESS-ABCD-1234-EFGH-5678"})
    assert r.status_code == 401


def test_license_verify_l5_success_openit():
    token = _login()
    c = _client()
    r = c.post("/v1/license/verify", json={"license_key": "OPENIT-BUSINESS-ABCD-1234-EFGH-5678"}, headers=_auth(token))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "valid"
    assert data["edition"] == "Business"
    assert data["bsl_version"] == "1.1"
    assert data["expires_at"] is not None
    assert data["holder"] is not None


def test_license_verify_l5_success_bsl():
    token = _login()
    c = _client()
    r = c.post("/v1/license/verify", json={"license_key": "BSL-1.1-BUSINESS-XYZ1-YYYY-ZZZZ-9999"}, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "valid"


def test_license_verify_invalid_format_400():
    token = _login()
    c = _client()
    r = c.post("/v1/license/verify", json={"license_key": "INVALID-KEY-123"}, headers=_auth(token))
    assert r.status_code == 400
    assert "Invalid license format" in r.text


def test_license_verify_placeholder_rejects_403():
    token = _login()
    c = _client()
    for bad in ["OPENIT-BUSINESS-DEMO-1234-ABCD-5678", "OPENIT-BUSINESS-INVALID-TEST-KEY-1234", "BSL-1.1-BUSINESS-EXPIRED-1234-ABCD-5678"]:
        r = c.post("/v1/license/verify", json={"license_key": bad}, headers=_auth(token))
        assert r.status_code == 403, f"expected 403 for {bad}: {r.text}"
        assert "placeholder/expired" in r.text.lower() or "rejected" in r.text.lower()


def test_license_verify_l4_forbidden():
    token_l4, _ = _make_l4()
    c = _client()
    r = c.post("/v1/license/verify", json={"license_key": "OPENIT-BUSINESS-ABCD-1234-EFGH-5678"}, headers=_auth(token_l4))
    assert r.status_code == 403


def test_license_status_unlicensed_initially():
    token = _login()
    c = _client()
    r = c.get("/v1/license/status", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["status"] == "unlicensed"


def test_license_status_l4_allowed():
    token_l4, _ = _make_l4()
    c = _client()
    r = c.get("/v1/license/status", headers=_auth(token_l4))
    assert r.status_code == 200


def test_license_status_requires_auth():
    c = _client()
    r = c.get("/v1/license/status")
    assert r.status_code == 401


def test_license_status_after_verify():
    token = _login()
    c = _client()
    c.post("/v1/license/verify", json={"license_key": "OPENIT-BUSINESS-ABCD-1234-EFGH-5678"}, headers=_auth(token))
    r = c.get("/v1/license/status", headers=_auth(token))
    assert r.json()["status"] == "valid"
    assert r.json()["license_key"] == "OPENIT-BUSINESS-ABCD-1234-EFGH-5678"


# ---------------------------------------------------------------------------
# Security updates
# ---------------------------------------------------------------------------

def test_security_updates_requires_auth():
    c = _client()
    r = c.get("/v1/security/updates")
    assert r.status_code == 401


def test_security_updates_viewer_allowed():
    token_l4, _ = _make_l4()
    c = _client()
    r = c.get("/v1/security/updates", headers=_auth(token_l4))
    assert r.status_code == 200


def test_security_updates_structure():
    token = _login()
    c = _client()
    r = c.get("/v1/security/updates", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert data["current_version"] == "0.1.1"
    assert data["count"] == 2
    assert len(data["updates"]) == 2
    for upd in data["updates"]:
        assert "version" in upd
        assert "cves" in upd
        assert "severity" in upd
        assert "changelog" in upd
        for cve in upd["cves"]:
            assert cve["id"].startswith("CVE-")
            assert "severity" in cve
            assert "summary" in cve


# ---------------------------------------------------------------------------
# Backup trigger + status (§16A.3.1 30-day retention)
# ---------------------------------------------------------------------------

def test_backup_status_requires_auth():
    c = _client()
    r = c.get("/v1/backup/status")
    assert r.status_code == 401


def test_backup_status_retention_policy():
    token = _login()
    c = _client()
    r = c.get("/v1/backup/status", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert data["retention_days"] == 30
    assert "30" in data["retention_policy"] or "16A" in data["retention_policy"]
    assert data["total"] == 0
    assert isinstance(data["backups"], list)
    assert "next_scheduled" in data


def test_backup_trigger_l5_success():
    token = _login()
    c = _client()
    r = c.post("/v1/backup/trigger", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "triggered"
    bkp = r.json()["backup"]
    assert bkp["status"] == "completed"
    assert bkp["retention_days"] == 30
    assert bkp["triggered_by"] == "admin@openit.co.kr"
    assert "expires_at" in bkp
    # verify appears in status
    r2 = c.get("/v1/backup/status", headers=_auth(token))
    assert r2.json()["total"] == 1


def test_backup_trigger_l4_forbidden():
    token_l4, _ = _make_l4()
    c = _client()
    r = c.post("/v1/backup/trigger", headers=_auth(token_l4))
    assert r.status_code == 403


def test_backup_trigger_requires_auth():
    c = _client()
    r = c.post("/v1/backup/trigger")
    assert r.status_code == 401


def test_backup_history_ordering():
    token = _login()
    c = _client()
    for _ in range(2):
        c.post("/v1/backup/trigger", headers=_auth(token))
    r = c.get("/v1/backup/status", headers=_auth(token))
    assert r.json()["total"] == 2
    backups = r.json()["backups"]
    # sorted reverse by created_at
    assert backups[0]["created_at"] >= backups[1]["created_at"]


# ---------------------------------------------------------------------------
# Upgrade status
# ---------------------------------------------------------------------------

def test_upgrade_status_requires_auth():
    c = _client()
    r = c.get("/v1/upgrade/status")
    assert r.status_code == 401


def test_upgrade_status_viewer_allowed():
    token_l4, _ = _make_l4()
    c = _client()
    r = c.get("/v1/upgrade/status", headers=_auth(token_l4))
    assert r.status_code == 200
    assert r.json()["current_version"] == "0.1.1"
    assert r.json()["available_version"] == "0.2.0"
    assert r.json()["status"] == "idle"


def test_upgrade_status_l5_allowed():
    token = _login()
    c = _client()
    r = c.get("/v1/upgrade/status", headers=_auth(token))
    assert r.status_code == 200
    assert "changelog" in r.json()
    assert "last_check" in r.json()
