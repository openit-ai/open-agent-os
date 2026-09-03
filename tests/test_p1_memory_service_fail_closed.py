"""P1: production memory service must fail closed on missing DB."""
from __future__ import annotations
import importlib.util
import time
import uuid
from pathlib import Path
from fastapi.testclient import TestClient
from jose import jwt

ROOT = Path(__file__).resolve().parents[1]
KEY = "test-unified-oaos-signing-key-32bytes-long-enough!!"

def _load():
    spec = importlib.util.spec_from_file_location("memory_service.fail_closed_test_app", ROOT / "memory_service" / "app.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def _token(scope: str) -> str:
    now = int(time.time())
    return jwt.encode({"iss":"open-agent-os-auth","aud":"memory-service","sub":"employee:kim","tenant_id":"acme","agent_id":"agent:assistant:kim","scope":scope,"exp":now+300,"iat":now,"jti":uuid.uuid4().hex}, KEY, algorithm="HS256")

def test_production_write_and_search_reject_missing_db(monkeypatch):
    mod = _load()
    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    client = TestClient(mod.app)
    assert client.post("/v1/memory/write", json={"content":"must not fallback"}, headers={"Authorization": f"Bearer {_token('memory:write')}"}).status_code == 503
    assert client.post("/v1/memory/search", json={"query":"x"}, headers={"Authorization": f"Bearer {_token('memory:read')}"}).status_code == 503
    assert client.get("/health").status_code == 200
    assert client.get("/readyz").status_code == 503

def test_readyz_reports_configured_db_failure_without_hanging(monkeypatch):
    mod = _load()
    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://bad:bad@127.0.0.1:59999/bad")
    monkeypatch.setenv("OAOS_HEALTHCHECK_TIMEOUT_SECONDS", "0.2")
    response = TestClient(mod.app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database"]["status"] in {"failed", "missing"}
    assert TestClient(mod.app).get("/health").status_code == 200
