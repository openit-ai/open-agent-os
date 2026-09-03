"""Admin P3 ops surfaces — profile-ops / knowledge-ops API tests (non-production)."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"

os.environ["OAOS_ENV"] = "test"
os.environ.pop("OAOS_DATABASE_URL", None)
os.environ["DATABASE_URL"] = "/tmp/oaos_p3_ops_test.db"
for k in ("OAOS_PROFILE_RESET_CONFIRM",):
    os.environ.pop(k, None)


def _load(name: str, filename: str, bare_alias: str | None = None):
    added = False
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
        added = True
    try:
        spec = importlib.util.spec_from_file_location(name, str(BACKEND / filename))
        mod = importlib.util.module_from_spec(spec)  # type: ignore
        sys.modules[name] = mod
        if bare_alias and bare_alias not in sys.modules:
            sys.modules[bare_alias] = mod
        spec.loader.exec_module(mod)  # type: ignore
        return mod
    finally:
        if added and str(BACKEND) in sys.path:
            sys.path.remove(str(BACKEND))


auth_mod = _load("p3_auth_mod", "auth.py", bare_alias="auth")
_app_mod = _load("p3_app_mod", "app.py")
app = _app_mod.app

P3_QNAMES = (
    "admin_console.backend.profile_ops",
    "admin_console.backend.knowledge_ops",
)


@pytest.fixture()
def client():
    for qn in P3_QNAMES:
        m = sys.modules.get(qn)
        assert m is not None, f"router module not mounted: {qn}"
        try:
            m.clear_pending_backfill_jobs()
        except AttributeError:
            pass
        try:
            m.clear_pending_sync_jobs()
        except AttributeError:
            pass
    auth_mod.clear_users()
    c = TestClient(app)
    yield c
    auth_mod.clear_users()


def _login(c: TestClient, email="admin@openit.co.kr", password="Admin123!") -> str:
    r = c.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(c: TestClient) -> dict:
    return {"Authorization": f"Bearer {_login(c)}"}


def test_openapi_has_p3_prefixes(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert any(p.startswith("/v1/profile-ops") for p in paths)
    assert any(p.startswith("/v1/knowledge-ops") for p in paths)


def test_profile_status_aggregation(client):
    h = _auth(client)
    mod = sys.modules["admin_console.backend.profile_ops"]
    mod.count_profiles = lambda tenant_id=None: (1, "db")  # type: ignore
    mod.count_traits = lambda tenant_id=None, user_id=None: (5, "db")  # type: ignore
    mod.count_evidence = lambda tenant_id=None, user_id=None: (3, "db")  # type: ignore
    mod.get_worker_queue_depth = lambda: (7, "worker-queue")  # type: ignore
    r = client.get("/v1/profile-ops/status?tenant_id=t1&user_id=u1", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile_exists"] is True
    assert body["profile_count"] == 1
    assert body["trait_count"] == 5
    assert body["evidence_count"] == 3
    assert body["worker_queue_depth"] == 7


def test_profile_backfill_enqueue(client):
    h = _auth(client)
    r = client.post(
        "/v1/profile-ops/backfill",
        json={"tenant_id": "t1", "user_id": "u1", "reason": "recompute"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enqueued"] is True
    assert body["job_id"]
    assert body["via"] in ("worker-queue", "local")


def test_profile_reset_requires_confirm_token(client):
    h = _auth(client)
    r = client.post(
        "/v1/profile-ops/reset", json={"tenant_id": "t1", "user_id": "u1"}, headers=h
    )
    assert r.status_code == 400, r.text
    r2 = client.post(
        "/v1/profile-ops/reset",
        json={"tenant_id": "t1", "user_id": "u1", "confirm": "WRONG"},
        headers=h,
    )
    assert r2.status_code == 400, r2.text


def test_profile_reset_delegates_with_token(client):
    h = _auth(client)
    r = client.post(
        "/v1/profile-ops/reset",
        json={"tenant_id": "t1", "user_id": "u1", "confirm": "RESET"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("delegated") is True


def test_knowledge_status_aggregation(client):
    h = _auth(client)
    mod = sys.modules["admin_console.backend.knowledge_ops"]
    mod.load_checkpoints = lambda tenant_id=None: (  # type: ignore
        [{"tenant_id": "t1", "source_system": "notion", "cursor": "c1",
          "last_sync_at": "2026-01-01", "updated_at": "2026-01-01"}], "db")
    mod.count_documents = lambda tenant_id=None: (42, "db")  # type: ignore
    r = client.get("/v1/knowledge-ops/status?tenant_id=t1", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkpoint_count"] == 1
    assert body["document_count"] == 42
    assert "notion" in body["synced_connectors"]
    assert "outline" in body["pending_connectors"]


def test_knowledge_sync_dry_run_enqueues_nothing(client):
    h = _auth(client)
    mod = sys.modules["admin_console.backend.knowledge_ops"]
    before = len(mod.list_pending_sync_jobs())
    r = client.post(
        "/v1/knowledge-ops/sync",
        json={"connector": "notion", "tenant_id": "t1", "dry_run": True},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["enqueued"] is False
    assert len(mod.list_pending_sync_jobs()) == before


def test_knowledge_sync_rejects_unknown_connector(client):
    h = _auth(client)
    r = client.post(
        "/v1/knowledge-ops/sync",
        json={"connector": "nope", "tenant_id": "t1"},
        headers=h,
    )
    assert r.status_code == 400, r.text
