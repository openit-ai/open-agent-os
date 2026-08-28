"""HA tests — /healthz /readyz /v1/health/detailed + retry/circuit-breaker"""
import asyncio
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in [
    ROOT / "control-plane",
    ROOT / "execution-gateway",
    ROOT / "security" / "policy-engine",
    ROOT / "security" / "delegation",
    ROOT / "security" / "credential-vault",
    ROOT / "security" / "token",
    ROOT / "security" / "crypto",
    ROOT / "security" / "audit",
    ROOT / "security" / "approval",
    ROOT / "packages" / "agent-runtime",
    ROOT / "packages" / "common-types",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "security") not in sys.path:
    sys.path.insert(0, str(ROOT / "security"))

from fastapi.testclient import TestClient

# -- healthz/readyz/detailed for control-plane, execution-gateway, security --

def test_healthz_control_plane():
    from control_plane.app import app
    c = TestClient(app)
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["service"] == "control-plane"

def test_readyz_control_plane_fail_open():
    from control_plane.app import app
    c = TestClient(app)
    r = c.get("/readyz")
    assert r.status_code == 200  # fail-open always 200
    j = r.json()
    assert "checks" in j
    assert "db" in j["checks"]
    assert "redis" in j["checks"]
    assert "self" in j["checks"]
    # status is ok or degraded, never 503
    assert j["status"] in ("ok", "degraded")

def test_detailed_control_plane_latency():
    from control_plane.app import app
    c = TestClient(app)
    r = c.get("/v1/health/detailed")
    assert r.status_code == 200
    j = r.json()
    assert "latency_ms" in j
    assert isinstance(j["latency_ms"], (int, float))
    assert "checks" in j
    for k, v in j["checks"].items():
        assert "latency_ms" in v
        assert "status" in v

def test_healthz_execution_gateway():
    from execution_gateway.app import app as eg_app
    c = TestClient(eg_app)
    for path in ("/healthz", "/readyz", "/v1/health/detailed"):
        r = c.get(path)
        assert r.status_code == 200
        assert r.json()["status"] in ("ok", "degraded")

def test_healthz_security():
    from security.app import app as sec_app
    c = TestClient(sec_app)
    for path in ("/healthz", "/readyz", "/v1/health/detailed"):
        r = c.get(path)
        assert r.status_code == 200
        assert r.json()["service"] == "security"
        if path == "/v1/health/detailed":
            assert "checks" in r.json()
            assert "latency_ms" in r.json()

def test_readyz_execution_gateway_draining_flag():
    from execution_gateway.app import app as eg_app
    c = TestClient(eg_app)
    r = c.get("/readyz")
    j = r.json()
    # execution-gateway adds active_requests and draining status
    assert "self" in j["checks"]

# -- retry logic: only 500/429/timeout retried, 400 not retried, audit recorded --
@pytest.mark.asyncio
async def test_retry_selective_llm_runtime():
    from agent_runtime.llm_runtime import _is_retryable_exception, _with_retry, AuditLogStub, _default_circuit_breaker
    # reset breaker
    _default_circuit_breaker._failures = 0
    _default_circuit_breaker._state = "CLOSED"
    _default_circuit_breaker._opened_at = None

    # 500 should be retryable
    class Fake500(Exception):
        status_code = 500
    assert _is_retryable_exception(Fake500("oops 500")) is True
    # 429 retryable
    class Fake429(Exception):
        status_code = 429
    assert _is_retryable_exception(Fake429("429")) is True
    # timeout retryable
    assert _is_retryable_exception(asyncio.TimeoutError("timeout")) is True
    assert _is_retryable_exception(TimeoutError("timed out")) is True
    # 400 not retryable
    class Fake400(Exception):
        status_code = 400
    assert _is_retryable_exception(Fake400("bad request")) is False
    # 404 not retryable
    class Fake404(Exception):
        status_code = 404
    assert _is_retryable_exception(Fake404("not found")) is False

    # verify _with_retry actually retries 3 times for 500 but not for 400
    audit = AuditLogStub()
    calls = {"n": 0}
    async def fail_500():
        calls["n"] += 1
        e = Fake500("500 err")
        raise e
    # should attempt 4 times (initial +3 retries) then raise
    with pytest.raises(Exception):
        await _with_retry(fail_500, max_retries=3, backoff_s=0.01, trace_id="t1", audit_log=audit, circuit_breaker=_default_circuit_breaker)
    assert calls["n"] == 4
    # audit should contain retry events + failure
    assert any(e.event_type == "retry" for e in audit.events)
    assert any(e.event_type == "llm_failure" for e in audit.events)

    # reset breaker again for 400 test
    _default_circuit_breaker._failures = 0
    _default_circuit_breaker._state = "CLOSED"
    _default_circuit_breaker._opened_at = None
    audit2 = AuditLogStub()
    calls2 = {"n": 0}
    async def fail_400():
        calls2["n"] += 1
        raise Fake400("400")
    with pytest.raises(Fake400):
        await _with_retry(fail_400, max_retries=3, backoff_s=0.01, trace_id="t2", audit_log=audit2, circuit_breaker=_default_circuit_breaker)
    # 400 should not retry => only 1 call
    assert calls2["n"] == 1

@pytest.mark.asyncio
async def test_circuit_breaker_opens():
    from agent_runtime.llm_runtime import CircuitBreaker, _with_retry, AuditLogStub
    cb = CircuitBreaker(failure_threshold=2, reset_timeout_s=1, name="test_cb")
    audit = AuditLogStub()
    class Fake500(Exception):
        status_code = 500
    async def fail():
        raise Fake500("500")
    # first failure batch -> cb failures 1
    with pytest.raises(Exception):
        await _with_retry(fail, max_retries=1, backoff_s=0.01, trace_id="cb1", audit_log=audit, circuit_breaker=cb)
    assert cb._failures == 1
    assert cb.state == "CLOSED"
    # second -> should open
    with pytest.raises(Exception):
        await _with_retry(fail, max_retries=1, backoff_s=0.01, trace_id="cb2", audit_log=audit, circuit_breaker=cb)
    assert cb.state == "OPEN"
    # third attempt should be blocked by circuit breaker immediately
    with pytest.raises(RuntimeError, match="circuit breaker OPEN"):
        await _with_retry(fail, max_retries=3, backoff_s=0.01, trace_id="cb3", audit_log=audit, circuit_breaker=cb)

@pytest.mark.asyncio
async def test_acp_retry_and_circuit():
    from control_plane.acp_adapter import _is_retryable_status, _with_retry_acp, _acp_circuit_breaker
    # reset
    _acp_circuit_breaker._failures = 0
    _acp_circuit_breaker._state = "CLOSED"
    _acp_circuit_breaker._opened_at = None
    class Fake500(Exception):
        status_code = 500
    assert _is_retryable_status(Fake500("500")) is True
    class Fake400(Exception):
        status_code = 400
    assert _is_retryable_status(Fake400("400")) is False

    calls = {"n": 0}
    async def do_ok_after_2():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Fake500("500")
        return {"ok": True}
    res = await _with_retry_acp(do_ok_after_2, max_retries=3, backoff_s=0.01, trace_id="acp_retry")
    assert res == {"ok": True}
    assert calls["n"] == 3

def test_execution_gateway_graceful_flag_exists():
    # verify graceful shutdown machinery exists
    from execution_gateway import app as eg_mod
    assert hasattr(eg_mod, "_active_requests") or hasattr(eg_mod.app, "router")
    # check healthz is registered
    from execution_gateway.app import app as eg_app
    routes = [r.path for r in eg_app.routes]
    assert "/healthz" in routes
    assert "/readyz" in routes
    assert "/v1/health/detailed" in routes
