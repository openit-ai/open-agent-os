"""Admin Console backend tests — auth, JWT, infra CRUD, health probe mock, RBAC."""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from security.models.orm import AdminInfraServiceORM

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "admin-console" / "backend"

# Import admin backend modules via importlib to avoid collision with security/app.py.
# We must alias bare `auth`/`infra` temporarily because backend source uses
# `from auth import ...` / `from infra import ...` fallback (see backend/*.py).
# Alias is cleaned up after import and via teardown; test_workstream_c now uses
# importlib absolute path so it is immune to this temporary pollution.

def _load_admin_module(name: str, filename: str, bare_alias: str | None = None):
    # Ensure backend dir is on sys.path during exec so `from auth import` resolves
    added_path = False
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
        added_path = True
    spec = importlib.util.spec_from_file_location(name, str(BACKEND / filename))
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    sys.modules[name] = mod
    if bare_alias:
        sys.modules[bare_alias] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore
    finally:
        # keep bare alias for runtime (infra/app depend on it) but remember to clean on teardown
        pass
    return mod

auth_mod = _load_admin_module("admin_auth", "auth.py", bare_alias="auth")
infra_mod = _load_admin_module("admin_infra", "infra.py", bare_alias="infra")
sys.modules["admin_console.backend.auth"] = auth_mod
sys.modules["admin_console.backend.infra"] = infra_mod
_app_mod = _load_admin_module("admin_app", "app.py")
# Remove BACKEND from front of path after import — security/app.py must win for
# bare `from app import` in test_workstream_c. Admin modules are already in
# sys.modules, so no further sys.path needed.
if str(BACKEND) in sys.path:
    sys.path.remove(str(BACKEND))
admin_app = _app_mod.app


@pytest.fixture(autouse=True)
def isolate_stores():
    """Reset in-memory stores before each test (preserve seed)."""
    auth_mod.clear_users()
    infra_mod.clear_services()
    yield
    auth_mod.clear_users()
    infra_mod.clear_services()


def _client():
    return TestClient(admin_app)


def _login(email="admin@openit.co.kr", password="Admin123!"):
    c = _client()
    r = c.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Auth / JWT
# ---------------------------------------------------------------------------

def test_seed_admin_exists():
    assert auth_mod.get_user_by_email("admin@openit.co.kr") is not None


@pytest.mark.asyncio
async def test_admin_startup_starts_periodic_infra_probe(monkeypatch, caplog):
    monkeypatch.setenv("OAOS_INFRA_PROBE_ENABLED", "true")
    monkeypatch.setenv("OAOS_INFRA_PROBE_INTERVAL_SECONDS", "47")
    monkeypatch.setattr(_app_mod, "ensure_admin_tables", AsyncMock())
    started_task = MagicMock()
    start = MagicMock(return_value=started_task)
    monkeypatch.setattr(infra_mod, "start_periodic_check", start)

    with caplog.at_level("INFO"):
        await _app_mod._admin_persistence_startup()

    start.assert_called_once_with(47)
    assert "Periodic infra probe started (interval_seconds=47)" in caplog.text


@pytest.mark.asyncio
async def test_admin_startup_isolates_periodic_probe_failure(monkeypatch, caplog):
    monkeypatch.setenv("OAOS_INFRA_PROBE_ENABLED", "true")
    monkeypatch.setattr(_app_mod, "ensure_admin_tables", AsyncMock())
    monkeypatch.setattr(infra_mod, "start_periodic_check", MagicMock(side_effect=OSError("boom")))

    with caplog.at_level("WARNING"):
        await _app_mod._admin_persistence_startup()

    assert "Periodic infra probe failed to start: OSError" in caplog.text


@pytest.mark.asyncio
async def test_admin_startup_seeds_canonical_registry(monkeypatch, caplog):
    monkeypatch.setenv("OAOS_INFRA_AUTO_SEED_ENABLED", "true")
    monkeypatch.setenv("OAOS_INFRA_PROBE_ENABLED", "false")
    monkeypatch.setattr(_app_mod, "ensure_admin_tables", AsyncMock())
    seed = MagicMock(return_value={"created_count": 6, "skipped_count": 4})
    monkeypatch.setattr(infra_mod, "ensure_canonical_registry", seed)

    with caplog.at_level("INFO"):
        await _app_mod._admin_persistence_startup()

    seed.assert_called_once_with()
    assert "Canonical infra registry ready (created=6 skipped=4)" in caplog.text


def test_login_success_and_me():
    token = _login()
    c = _client()
    r = c.get("/v1/auth/me", headers=_auth_header(token))
    assert r.status_code == 200
    assert r.json()["email"] == "admin@openit.co.kr"
    assert r.json()["role"] == "L5"


def test_login_wrong_password():
    c = _client()
    r = c.post("/v1/auth/login", json={"email": "admin@openit.co.kr", "password": "wrongpass123"})
    assert r.status_code == 401


def test_jwt_invalid_rejected():
    c = _client()
    r = c.get("/v1/auth/me", headers=_auth_header("invalid.token.here"))
    assert r.status_code == 401


def test_jwt_missing_rejected():
    c = _client()
    r = c.get("/v1/auth/me")
    assert r.status_code == 401


def test_register_requires_L5():
    # L5 seed creates L4 user
    token_l5 = _login()
    c = _client()
    r = c.post(
        "/v1/auth/register",
        json={"email": "l4@test.co.kr", "password": "Password123!", "display_name": "L4 User", "role": "L4"},
        headers=_auth_header(token_l5),
    )
    assert r.status_code == 201, r.text
    # login as L4
    r2 = c.post("/v1/auth/login", json={"email": "l4@test.co.kr", "password": "Password123!"})
    token_l4 = r2.json()["access_token"]
    # L4 tries to register -> 403
    r3 = c.post(
        "/v1/auth/register",
        json={"email": "another@test.co.kr", "password": "Password123!", "display_name": "Another", "role": "L4"},
        headers=_auth_header(token_l4),
    )
    assert r3.status_code == 403


def test_register_duplicate_email():
    token = _login()
    c = _client()
    c.post(
        "/v1/auth/register",
        json={"email": "dup@test.co.kr", "password": "Password123!", "display_name": "Dup", "role": "L4"},
        headers=_auth_header(token),
    )
    r = c.post(
        "/v1/auth/register",
        json={"email": "dup@test.co.kr", "password": "Password123!", "display_name": "Dup2", "role": "L4"},
        headers=_auth_header(token),
    )
    assert r.status_code == 409


def test_bcrypt_hash_not_plaintext():
    token = _login()
    c = _client()
    c.post(
        "/v1/auth/register",
        json={"email": "chk@test.co.kr", "password": "Password123!", "display_name": "Chk", "role": "L4"},
        headers=_auth_header(token),
    )
    user = auth_mod.get_user_by_email("chk@test.co.kr")
    assert user is not None
    assert user.hashed_password != "Password123!"
    # Argon2id for new hashes when available, bcrypt fallback otherwise — both are valid
    assert user.hashed_password.startswith("$2") or user.hashed_password.startswith("$argon2")


# ---------------------------------------------------------------------------
# Infra CRUD
# ---------------------------------------------------------------------------

def test_infra_crud_flow():
    token = _login()
    c = _client()
    h = _auth_header(token)

    # create
    r = c.post(
        "/v1/infra/services",
        json={"name": "hermes", "display_name": "Hermes Runtime", "host": "127.0.0.1", "port": 8001},
        headers=h,
    )
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    # list
    r2 = c.get("/v1/infra/services", headers=h)
    assert r2.status_code == 200
    assert any(s["id"] == sid for s in r2.json())

    # get one
    r3 = c.get(f"/v1/infra/services/{sid}", headers=h)
    assert r3.status_code == 200
    assert r3.json()["name"] == "hermes"

    # update
    r4 = c.put(f"/v1/infra/services/{sid}", json={"display_name": "Hermes Updated"}, headers=h)
    assert r4.status_code == 200
    assert r4.json()["display_name"] == "Hermes Updated"

    # delete
    r5 = c.delete(f"/v1/infra/services/{sid}", headers=h)
    assert r5.status_code == 200
    r6 = c.get(f"/v1/infra/services/{sid}", headers=h)
    assert r6.status_code == 404


def test_infra_invalid_name():
    token = _login()
    c = _client()
    r = c.post(
        "/v1/infra/services",
        json={"name": "invalid-service", "display_name": "Bad", "host": "127.0.0.1", "port": 8000},
        headers=_auth_header(token),
    )
    assert r.status_code == 400


def test_infra_crud_requires_auth():
    c = _client()
    r = c.get("/v1/infra/services")
    assert r.status_code == 401
    r2 = c.post("/v1/infra/services", json={"name": "hermes", "display_name": "X", "host": "127.0.0.1", "port": 8000})
    assert r2.status_code == 401


def test_infra_l4_read_allowed_write_denied():
    # create L4 user via L5
    token_l5 = _login()
    c = _client()
    c.post(
        "/v1/auth/register",
        json={"email": "reader@test.co.kr", "password": "Password123!", "display_name": "Reader", "role": "L4"},
        headers=_auth_header(token_l5),
    )
    # create a service as L5 for read test
    c.post(
        "/v1/infra/services",
        json={"name": "mattermost", "display_name": "MM", "host": "127.0.0.1", "port": 8002},
        headers=_auth_header(token_l5),
    )
    # login L4
    r = c.post("/v1/auth/login", json={"email": "reader@test.co.kr", "password": "Password123!"})
    token_l4 = r.json()["access_token"]
    h4 = _auth_header(token_l4)

    # L4 read allowed
    r2 = c.get("/v1/infra/services", headers=h4)
    assert r2.status_code == 200

    # L4 write denied
    r3 = c.post(
        "/v1/infra/services",
        json={"name": "outline", "display_name": "Outline", "host": "127.0.0.1", "port": 8003},
        headers=h4,
    )
    assert r3.status_code == 403

    r4 = c.delete("/v1/infra/services/infra_fake", headers=h4)
    assert r4.status_code == 403  # blocked before 404 due to role check


# ---------------------------------------------------------------------------
# Health probe mock
# ---------------------------------------------------------------------------

def test_health_probe_mock_healthy():
    token = _login()
    c = _client()
    h = _auth_header(token)
    # register two services
    c.post("/v1/infra/services", json={"name": "hermes", "display_name": "Hermes", "host": "127.0.0.1", "port": 9001}, headers=h)
    c.post("/v1/infra/services", json={"name": "outline", "display_name": "Outline", "host": "127.0.0.1", "port": 9002}, headers=h)

    # mock httpx.AsyncClient.get to return 200
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        r = c.get("/v1/infra/health", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert len(data["services"]) == 2
    for s in data["services"]:
        assert s["status"] == "healthy"
        assert s["latency_ms"] is not None
        assert s["last_check"] is not None


def test_health_probe_unhealthy_on_exception():
    token = _login()
    c = _client()
    h = _auth_header(token)
    c.post("/v1/infra/services", json={"name": "control-plane", "display_name": "CP", "host": "127.0.0.1", "port": 9003}, headers=h)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        r = c.get("/v1/infra/health", headers=h)
    assert r.status_code == 200
    assert r.json()["services"][0]["status"] == "unhealthy"


def test_health_probe_audit_event():
    token = _login()
    c = _client()
    h = _auth_header(token)
    c.post("/v1/infra/services", json={"name": "execution-gateway", "display_name": "EGW", "host": "127.0.0.1", "port": 9004}, headers=h)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        c.get("/v1/infra/health", headers=h)

    # audit events recorded
    r2 = c.get("/v1/infra/audit/events", headers=h)
    assert r2.status_code == 200
    assert len(r2.json()["events"]) >= 1


def test_canonical_seed_is_idempotent_and_preserves_existing_and_legacy_rows(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'canonical-seed.db'}"
    monkeypatch.setenv("OAOS_DATABASE_URL", database_url)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    infra_mod._db_engine = None
    infra_mod._db_session_factory = None
    engine = create_engine(database_url)
    AdminInfraServiceORM.__table__.create(engine)
    existing = infra_mod.InfraService(
        id="operator_mattermost", name="mattermost", display_name="Operator Mattermost",
        host="chat.operator.internal", port=9443, health_path="/custom-health",
        status=infra_mod.InfraStatus.healthy, latency_ms=7.5,
        last_check=datetime(2026, 9, 15, 11, 0, tzinfo=UTC),
    )
    legacy = infra_mod.InfraService(
        id="legacy_security", name="security", display_name="Legacy Security",
        host="security.internal", port=9000,
    )
    with Session(engine) as session:
        for service in (existing, legacy):
            session.add(AdminInfraServiceORM(
                id=service.id, name=service.name, display_name=service.display_name,
                host=service.host, port=service.port, health_path=service.health_path,
                expected_status=service.expected_status, status=service.status.value,
                latency_ms=service.latency_ms, last_check=service.last_check, extra=None,
            ))
        session.commit()

    first = infra_mod.ensure_canonical_registry()
    second = infra_mod.ensure_canonical_registry()
    by_name = {service.name: service for service in infra_mod._db_list_services()}

    assert first["created_count"] == 9
    assert first["skipped"] == ["mattermost"]
    assert second["created_count"] == 0
    assert second["skipped_count"] == len(infra_mod.CANONICAL_ORDER)
    assert set(infra_mod.CANONICAL_ORDER).issubset(by_name)
    preserved = by_name["mattermost"]
    assert (
        preserved.id, preserved.display_name, preserved.host, preserved.port,
        preserved.health_path, preserved.status, preserved.latency_ms,
    ) == (
        existing.id, existing.display_name, existing.host, existing.port,
        existing.health_path, existing.status, existing.latency_ms,
    )
    assert preserved.last_check.replace(tzinfo=UTC) == existing.last_check
    assert by_name["security"].id == legacy.id
    assert by_name["security"].host == legacy.host
    if infra_mod._db_engine is not None:
        infra_mod._db_engine.dispose()
    infra_mod._db_engine = None
    infra_mod._db_session_factory = None
    engine.dispose()


@pytest.mark.asyncio
async def test_seeded_control_plane_is_probed_and_reaches_readiness_snapshot(monkeypatch):
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    if infra_mod._db_engine is not None:
        infra_mod._db_engine.dispose()
    infra_mod._db_engine = None
    infra_mod._db_session_factory = None
    infra_mod.ensure_canonical_registry()
    readiness_mod = _app_mod._readiness_mod
    original_domain = readiness_mod._domain
    monkeypatch.setattr(
        readiness_mod, "_domain",
        lambda name: infra_mod if name == "infra" else original_domain(name),
    )
    readiness_mod._recent_tests.clear()
    infra_mod.register_readiness_observation_sink(readiness_mod._record_observation)
    checked_at = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    probed_names = []

    async def healthy_probe(candidate):
        probed_names.append(candidate.name)
        candidate.status = infra_mod.InfraStatus.healthy
        candidate.last_check = checked_at
        candidate.latency_ms = 2.0
        return candidate

    monkeypatch.setattr(infra_mod, "_probe_one", healthy_probe)
    await infra_mod.probe_all_services()
    snapshot = readiness_mod._collect_snapshot(now=checked_at)

    assert set(infra_mod.CANONICAL_ORDER).issubset(probed_names)
    assert "control-plane" in probed_names
    assert snapshot["environment"]["control_plane"] == {
        "ok": True, "code": "OK", "checked_at": checked_at.isoformat(),
    }


def test_connection_observation_persists_in_infra_registry_database(tmp_path, monkeypatch):
    db_path = tmp_path / "readiness-observations.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("OAOS_DATABASE_URL", database_url)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    infra_mod._db_engine = None
    infra_mod._db_session_factory = None

    engine = create_engine(database_url)
    AdminInfraServiceORM.__table__.create(engine)
    with Session(engine) as session:
        session.add(AdminInfraServiceORM(
            id="infra_admin_api", name="admin-api", display_name="Admin API",
            host="127.0.0.1", port=8010, health_path="/health", expected_status=200,
            status="unknown", latency_ms=None, last_check=None, extra=None,
        ))
        session.commit()

    checked_at = "2026-09-15T12:00:00+00:00"
    observation = {
        "ok": True, "status": "healthy", "code": "OK", "checked_at": checked_at,
        "latency_ms": 4.2, "target_display": "service.internal:443",
    }
    persisted_observation = {**observation, "last_success_at": checked_at}
    readiness_mod = _app_mod._readiness_mod
    original_domain = readiness_mod._domain
    monkeypatch.setattr(
        readiness_mod, "_domain",
        lambda name: infra_mod if name == "infra" else original_domain(name),
    )
    readiness_mod._record_observation("mattermost", observation)
    readiness_mod._recent_tests.clear()  # simulate an admin-api process restart

    assert readiness_mod._redacted_observation("mattermost") == persisted_observation
    with Session(engine) as session:
        row = session.get(AdminInfraServiceORM, "infra_admin_api")
        assert row is not None
        assert row.extra["readiness_observations"]["mattermost"] == persisted_observation

    if infra_mod._db_engine is not None:
        infra_mod._db_engine.dispose()
    infra_mod._db_engine = None
    infra_mod._db_session_factory = None
    engine.dispose()


@pytest.mark.asyncio
async def test_periodic_probe_refreshes_readiness_observation_within_ttl(monkeypatch):
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OAOS_ADMIN_READINESS_TTL_SECONDS", "300")
    readiness_mod = _app_mod._readiness_mod
    readiness_mod._recent_tests.clear()
    infra_mod.register_readiness_observation_sink(readiness_mod._record_observation)
    checked_at = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    service = infra_mod.InfraService(
        id="infra_mattermost", name="mattermost", display_name="Mattermost",
        host="mattermost.internal", port=8065,
    )
    infra_mod._services[service.id] = service

    async def healthy_probe(candidate):
        candidate.status = infra_mod.InfraStatus.healthy
        candidate.last_check = checked_at
        candidate.latency_ms = 3.0
        return candidate

    monkeypatch.setattr(infra_mod, "_probe_one", healthy_probe)
    await infra_mod.probe_all_services()

    observation = readiness_mod._redacted_observation("mattermost")
    assert observation is not None
    assert readiness_mod._classify(
        required=True, configured=True, applied=True, observation=observation,
        now=checked_at + timedelta(seconds=299),
    ) == ("healthy", "OK")


# ---------------------------------------------------------------------------
# Dashboard proxy
# ---------------------------------------------------------------------------

def test_dashboard_stats_requires_auth():
    c = _client()
    r = c.get("/v1/dashboard/stats")
    assert r.status_code == 401


def test_dashboard_stats_ok():
    token = _login()
    c = _client()
    r = c.get("/v1/dashboard/stats", headers=_auth_header(token))
    assert r.status_code == 200
    data = r.json()
    assert "users_count" in data
    assert "audit_count" in data
    assert "infra_services_count" in data
