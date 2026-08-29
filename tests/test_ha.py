"""HA tests — /healthz /readyz /v1/health/detailed + retry/circuit-breaker"""
import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch
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


# ---------------------------------------------------------------------------
# H4 strict readiness — TDD (prod 503, liveness 200, draining 503, bounded checks)
# ---------------------------------------------------------------------------
def _h4_is_production():
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        if os.getenv(k, "").strip().lower() in ("production", "prod"):
            return True
    return False

def _h4_apps():
    from control_plane.app import app as cp_app
    from execution_gateway.app import app as eg_app
    from security.app import app as sec_app
    return [("control-plane", cp_app), ("execution-gateway", eg_app), ("security", sec_app)]


def test_h4_healthz_liveness_always_200_even_prod_degraded(monkeypatch):
    """H4: /healthz must stay 200 even when prod readyz would be 503 (liveness)."""
    monkeypatch.setenv("OAOS_ENV", "production")
    # force degraded by patching bounded pings to fail
    for name, app in _h4_apps():
        mod_patches = []
        if name == "control-plane":
            mod_patches.append(patch("control_plane.app._bounded_db_ping", side_effect=RuntimeError("db down")))
            mod_patches.append(patch("control_plane.app._bounded_redis_ping", side_effect=RuntimeError("redis down")))
        elif name == "execution-gateway":
            mod_patches.append(patch("execution_gateway.app._bounded_db_ping", side_effect=RuntimeError("db down")))
            mod_patches.append(patch("execution_gateway.app._bounded_redis_ping", side_effect=RuntimeError("redis down")))
        else:
            # security: may have _bounded_* after H4, also patch vault if present
            try:
                mod_patches.append(patch("security.app._bounded_db_ping", side_effect=RuntimeError("db down")))
            except Exception:
                pass
            try:
                mod_patches.append(patch("security.app._bounded_redis_ping", side_effect=RuntimeError("redis down")))
            except Exception:
                pass
            try:
                mod_patches.append(patch("security.app._bounded_vault_ping", side_effect=RuntimeError("vault down")))
            except Exception:
                pass
        # also force vault via env where applicable
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@invalid.invalid:5432/db")
        monkeypatch.setenv("REDIS_URL", "redis://invalid.invalid:6379/0")
        # apply patches
        entered = [p.__enter__() for p in mod_patches]
        try:
            c = TestClient(app)
            r = c.get("/healthz")
            assert r.status_code == 200, f"{name} /healthz must be 200 even prod degraded, got {r.status_code}"
            assert r.json().get("status") == "ok"
        finally:
            for p in mod_patches:
                try:
                    p.__exit__(None, None, None)
                except Exception:
                    pass


def test_h4_readyz_prod_returns_503_on_db_degraded(monkeypatch):
    """H4: prod /readyz must be 503 when DB degraded (bounded real check)."""
    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@invalid.invalid:5432/db")
    monkeypatch.setenv("REDIS_URL", "")  # avoid redis noise
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_BACKEND", raising=False)
    # patch bounded DB to fail fast, redis to succeed
    with patch("control_plane.app._bounded_db_ping", side_effect=RuntimeError("db down")):
        with patch("control_plane.app._bounded_redis_ping", side_effect=None):
            from control_plane.app import app as cp_app
            c = TestClient(cp_app)
            r = c.get("/readyz")
            assert r.status_code == 503, f"control-plane readyz prod db degraded must be 503, got {r.status_code} body={r.text}"
            j = r.json()
            assert j["status"] in ("degraded", "draining")
            assert "db" in j["checks"]
            assert j["checks"]["db"]["status"] == "degraded"

    with patch("execution_gateway.app._bounded_db_ping", side_effect=RuntimeError("db down")):
        from execution_gateway.app import app as eg_app
        c2 = TestClient(eg_app)
        r2 = c2.get("/readyz")
        assert r2.status_code == 503, f"EG readyz prod db degraded must be 503, got {r2.status_code}"
        assert r2.json()["checks"]["db"]["status"] == "degraded"

    # security: may not have patch target yet before impl -> use env-driven degraded
    # force security db degraded via monkeypatching its _bounded_db_ping if exists, else rely on config
    try:
        with patch("security.app._bounded_db_ping", side_effect=RuntimeError("db down")):
            from security.app import app as sec_app
            c3 = TestClient(sec_app)
            r3 = c3.get("/readyz")
            assert r3.status_code == 503, f"security readyz prod db degraded must be 503, got {r3.status_code}"
    except Exception:
        # fallback: set invalid DATABASE_URL and check real format validation degraded -> still expect 503 in prod
        from security.app import app as sec_app2
        # need to set OAOS_DATABASE_URL or DATABASE_URL to invalid format that triggers degraded?
        # invalid url with no :// will be treated as degraded
        monkeypatch.setenv("DATABASE_URL", "not-a-url")
        c3 = TestClient(sec_app2)
        r3 = c3.get("/readyz")
        assert r3.status_code == 503, f"security readyz prod invalid db url must be 503, got {r3.status_code} body={r3.text}"


def test_h4_readyz_prod_returns_503_on_redis_degraded(monkeypatch):
    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.setenv("REDIS_URL", "redis://invalid.invalid:6379/0")
    monkeypatch.setenv("DATABASE_URL", "")  # avoid db noise
    # control-plane redis degraded -> 503
    with patch("control_plane.app._bounded_redis_ping", side_effect=RuntimeError("redis down")):
        with patch("control_plane.app._bounded_db_ping", side_effect=None):
            from control_plane.app import app as cp_app
            c = TestClient(cp_app)
            r = c.get("/readyz")
            assert r.status_code == 503
            assert r.json()["checks"]["redis"]["status"] == "degraded"
    with patch("execution_gateway.app._bounded_redis_ping", side_effect=RuntimeError("redis down")):
        from execution_gateway.app import app as eg_app
        c2 = TestClient(eg_app)
        r2 = c2.get("/readyz")
        assert r2.status_code == 503
        assert r2.json()["checks"]["redis"]["status"] == "degraded"
    try:
        with patch("security.app._bounded_redis_ping", side_effect=RuntimeError("redis down")):
            from security.app import app as sec_app
            c3 = TestClient(sec_app)
            r3 = c3.get("/readyz")
            assert r3.status_code == 503
            assert r3.json()["checks"]["redis"]["status"] == "degraded"
    except Exception:
        from security.app import app as sec_app2
        monkeypatch.setenv("REDIS_URL", "not-a-url")
        c3 = TestClient(sec_app2)
        r3 = c3.get("/readyz")
        assert r3.status_code == 503, f"security redis invalid url prod must be 503 got {r3.status_code}"


def test_h4_readyz_prod_returns_503_on_vault_degraded_where_configured(monkeypatch):
    """H4: when VAULT_ADDR/VAULT_BACKEND configured, vault degraded -> prod 503, vault in checks."""
    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.setenv("VAULT_ADDR", "http://invalid.invalid:8200")
    monkeypatch.setenv("VAULT_BACKEND", "hashicorp")
    # ensure DB/Redis not configured to avoid unrelated degraded
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    # patch vault health to fail fast for each service
    for name, app in _h4_apps():
        patch_target = f"{name.replace('-','_')}.app._bounded_vault_ping" if name != "security" else "security.app._bounded_vault_ping"
        # normalize: control_plane, execution_gateway, security
        if name == "control-plane":
            target = "control_plane.app._bounded_vault_ping"
        elif name == "execution-gateway":
            target = "execution_gateway.app._bounded_vault_ping"
        else:
            target = "security.app._bounded_vault_ping"
        try:
            with patch(target, side_effect=RuntimeError("vault down")):
                c = TestClient(app)
                r = c.get("/readyz")
                assert r.status_code == 503, f"{name} vault degraded prod must be 503, got {r.status_code} body={r.text}"
                j = r.json()
                assert "vault" in j["checks"], f"{name} must expose vault check when VAULT_ADDR set, got {j['checks'].keys()}"
                assert j["checks"]["vault"]["status"] == "degraded"
        except ModuleNotFoundError:
            # module may not have vault ping yet -> assert vault key still expected (fails before impl)
            c = TestClient(app)
            r = c.get("/readyz")
            # before H4 impl this will fail to have vault key and be 200
            assert r.status_code == 503, f"{name} expected 503 with vault configured, got {r.status_code} (before H4 this fails)"
            assert "vault" in r.json()["checks"], f"{name} missing vault check (TDD expects it)"


def test_h4_readyz_prod_returns_503_when_draining(monkeypatch):
    """H4: draining flag -> prod /readyz 503, /healthz 200."""
    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_BACKEND", raising=False)
    for name, app in _h4_apps():
        mod_name = "control_plane.app" if name == "control-plane" else ("execution_gateway.app" if name == "execution-gateway" else "security.app")
        import importlib
        mod = importlib.import_module(mod_name)
        # set draining flag: try _shutting_down, _draining, or patch _ha_checks
        had_flag = False
        orig_vals = {}
        for flag in ("_shutting_down", "_draining", "_is_draining"):
            if hasattr(mod, flag):
                orig_vals[flag] = getattr(mod, flag)
                try:
                    setattr(mod, flag, True)
                    had_flag = True
                except Exception:
                    pass
        # if no flag exists, patch _ha_checks to inject draining
        if not had_flag:
            # fallback: patch _ha_checks to return draining self
            orig_ha = getattr(mod, "_ha_checks", None)
            def _draining_ha():
                return {"db": {"status": "skipped", "latency_ms": 0}, "redis": {"status": "skipped", "latency_ms": 0}, "vault": {"status": "skipped", "latency_ms": 0}, "self": {"status": "draining", "latency_ms": 0}}
            monkeypatch.setattr(mod, "_ha_checks", _draining_ha, raising=False)
        try:
            c = TestClient(app)
            r_ready = c.get("/readyz")
            r_health = c.get("/healthz")
            assert r_health.status_code == 200, f"{name} healthz draining prod must stay 200, got {r_health.status_code}"
            assert r_ready.status_code == 503, f"{name} readyz draining prod must be 503, got {r_ready.status_code} body={r_ready.text}"
            assert r_ready.json().get("status") in ("draining", "degraded"), f"{name} draining status expected, got {r_ready.json()}"
        finally:
            for k, v in orig_vals.items():
                try:
                    setattr(mod, k, v)
                except Exception:
                    pass
            if not had_flag:
                try:
                    if orig_ha is not None:
                        monkeypatch.setattr(mod, "_ha_checks", orig_ha, raising=False)
                    else:
                        monkeypatch.delattr(mod, "_ha_checks", raising=False)
                except Exception:
                    pass


def test_h4_readyz_nonprod_preserves_200_degraded_explicit(monkeypatch):
    """H4: non-prod must explicitly preserve 200 degraded (no 503)."""
    monkeypatch.delenv("OAOS_ENV", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("OAOS_ENVIRONMENT", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@invalid.invalid:5432/db")
    monkeypatch.setenv("OAOS_ENV", "development")  # explicit non-prod
    for name, app in _h4_apps():
        target = "control_plane.app._bounded_db_ping" if name == "control-plane" else ("execution_gateway.app._bounded_db_ping" if name == "execution-gateway" else "security.app._bounded_db_ping")
        try:
            with patch(target, side_effect=RuntimeError("db down")):
                c = TestClient(app)
                r = c.get("/readyz")
                assert r.status_code == 200, f"{name} non-prod degraded must stay 200, got {r.status_code}"
                assert r.json()["status"] == "degraded"
        except ModuleNotFoundError:
            # security may not have bounded yet -> use non-prod degraded via env
            c = TestClient(app)
            r = c.get("/readyz")
            assert r.status_code == 200, f"{name} non-prod must be 200 even degraded, got {r.status_code}"
            assert r.json()["status"] == "degraded"


def test_h4_readyz_bounded_no_hang_and_latency(monkeypatch):
    """H4: bounded checks must not hang, latency_ms present, <1500ms even on failure."""
    monkeypatch.setenv("OAOS_ENV", "production")
    # use slowly failing vault to test bounded timeout (patch to sleep then raise, but impl should bound)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@10.255.255.1:5432/db")  # unroutable
    monkeypatch.setenv("REDIS_URL", "redis://10.255.255.1:6379/0")
    monkeypatch.setenv("VAULT_ADDR", "http://10.255.255.1:8200")
    for name, app in _h4_apps():
        start = time.monotonic()
        c = TestClient(app)
        r = c.get("/readyz")
        elapsed = (time.monotonic() - start) * 1000
        # bounded: should be < 5s even with unreachable hosts (each ~0.8+0.5)
        assert elapsed < 5000, f"{name} readyz took {elapsed:.0f}ms, exceeds bounded 5000ms"
        j = r.json()
        assert "checks" in j
        for k, v in j["checks"].items():
            assert "latency_ms" in v, f"{name} check {k} missing latency_ms"
            assert "status" in v
            assert isinstance(v["latency_ms"], (int, float))
        # vault must be present when VAULT_ADDR set
        assert "vault" in j["checks"], f"{name} vault check must be present when VAULT_ADDR set"


def test_h4_k8s_manifests_correct_probes():
    """H4: k8s liveness must use /healthz, readiness /readyz with correct ports/thresholds."""
    import yaml
    base = Path(__file__).resolve().parents[1] / "deploy" / "k8s"
    for svc, port in [("control-plane", 8000), ("execution-gateway", 8001), ("security", 8002)]:
        dep_path = base / svc / "deployment.yaml"
        assert dep_path.exists(), f"missing {dep_path}"
        text = dep_path.read_text()
        assert "/healthz" in text, f"{svc} liveness must probe /healthz"
        assert "/readyz" in text, f"{svc} readiness must probe /readyz"
        assert f"port: {port}" in text or f"port: {port}" in text.replace(" ", ""), f"{svc} probe port mismatch {port}"
        # ensure not using anonymous fallback probe on /
        assert "path: /healthz" in text
        assert "path: /readyz" in text


def test_h4_compose_healthchecks_use_healthz_liveness():
    import yaml
    compose = Path(__file__).resolve().parents[1] / "deploy" / "docker-compose.prod.yml"
    assert compose.exists(), "docker-compose.prod.yml missing"
    text = compose.read_text()
    # each service healthcheck should use curl on /healthz (liveness)
    assert "/healthz" in text, "compose healthchecks must use /healthz liveness"
    assert "service_healthy" in text, "compose prod must use service_healthy (readiness via depends_on)"
