"""H7 production mock immutable gate — TDD (design §9).

Invariants:
- I-H7-1: prod is immutable startup gate — mock/fallback/noop paths must be blocked in production
           even when OAOS_MOCK_FALLBACK=1 is set.  prod mock is never allowed.
- I-H7-2: llm_runtime / mcp_client / rate_limiter mock/noop branches fail-closed (503/RuntimeError) in prod
- I-H7-3: OAOS_ENV=production with 503 evidence, non-prod preserves WARNING fallback

5 tests per §9.7 (test_mock_fallback_hardening.py)
"""
import os
import importlib
import asyncio
from pathlib import Path
import pytest

def _set_env(**kwargs):
    old = {}
    for k, v in kwargs.items():
        old[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return old

def _restore(old):
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

ROOT = Path(__file__).resolve().parents[1]

def test_prod_mock_blocked():
    """I-H7-1: OAOS_ENV=production — is_mock_allowed() must be False even with OAOS_MOCK_FALLBACK=1 (immutable)."""
    old = _set_env(OAOS_ENV="production", OAOS_MOCK_FALLBACK="1", ENV=None, OAOS_ENVIRONMENT=None)
    try:
        # force reimport to pick up env
        import agent_runtime.env_gate as g
        importlib.reload(g)
        assert g.is_production() is True
        # IMMUTABLE: even explicit 1 must be blocked in production
        assert g.is_mock_allowed() is False, "prod must be immutable: OAOS_MOCK_FALLBACK=1 must NOT enable mock in production"
        # also without override
        os.environ.pop("OAOS_MOCK_FALLBACK", None)
        importlib.reload(g)
        assert g.is_mock_allowed() is False
        # mirrored gates must agree
        import execution_gateway.env_gate as eg
        importlib.reload(eg)
        assert eg.is_mock_allowed() is False
        import control_plane.env_gate as cg
        importlib.reload(cg)
        assert cg.is_mock_allowed() is False
    finally:
        _restore(old)
        import agent_runtime.env_gate as g2
        importlib.reload(g2)
        import execution_gateway.env_gate as eg2
        importlib.reload(eg2)
        import control_plane.env_gate as cg2
        importlib.reload(cg2)

def test_nonprod_mock_allowed():
    """non-prod (dev/test) must still allow mock fallback (fail-open with WARNING)."""
    old = _set_env(OAOS_ENV="development", OAOS_MOCK_FALLBACK=None, ENV=None)
    try:
        import agent_runtime.env_gate as g
        importlib.reload(g)
        assert g.is_production() is False
        assert g.is_mock_allowed() is True
        # explicit 0 should disable even in non-prod
        os.environ["OAOS_MOCK_FALLBACK"] = "0"
        importlib.reload(g)
        assert g.is_mock_allowed() is False
        # reset to empty -> allowed
        os.environ["OAOS_MOCK_FALLBACK"] = ""
        importlib.reload(g)
        assert g.is_mock_allowed() is True
    finally:
        _restore(old)
        import agent_runtime.env_gate as g2
        importlib.reload(g2)

def test_rate_limiter_noop_blocked_in_prod():
    """I-H7-2: ToolRateLimiter import failure must raise in prod, not silently allow (_Noop)."""
    old = _set_env(OAOS_ENV="production", OAOS_MOCK_FALLBACK=None)
    try:
        # Simulate import failure by temporarily corrupting tool_policy import
        import execution_gateway.app as app_mod
        importlib.reload(app_mod)
        # Patch import to simulate failure: monkeypatch tool_policy
        import unittest.mock as mock
        # Force _get_rate_limiter to hit except branch by making ToolRateLimiter import fail
        # We do this by reloading with a broken sys.modules entry
        original = app_mod._rate_limiter
        app_mod._rate_limiter = None
        with mock.patch.dict("sys.modules", {"execution_gateway.tool_policy": None}):
            # Need to force import inside function to fail; clear cache so function re-enters except
            app_mod._rate_limiter = None
            # The function does try: from .tool_policy import ToolRateLimiter — which will fail if module is None
            # In prod, it must raise RuntimeError, not return _Noop
            with pytest.raises(RuntimeError, match="rate limiter unavailable in production"):
                app_mod._get_rate_limiter()
        # cleanup
        app_mod._rate_limiter = original
        # non-prod should return _Noop and allow
        os.environ["OAOS_ENV"] = "development"
        importlib.reload(app_mod)
        app_mod._rate_limiter = None
        with mock.patch.dict("sys.modules", {"execution_gateway.tool_policy": None}):
            app_mod._rate_limiter = None
            limiter = app_mod._get_rate_limiter()
            assert limiter.allow("test-key") is True
            assert hasattr(limiter, "retry_after")
        app_mod._rate_limiter = original
    finally:
        _restore(old)
        import execution_gateway.app as app_mod2
        importlib.reload(app_mod2)
        app_mod2._rate_limiter = None

@pytest.mark.asyncio
async def test_mcp_gateway_unreachable_503_in_prod():
    """I-H7-2: MCP gateway unreachable must be 503/fail-closed in prod (no mock fallback)."""
    old = _set_env(OAOS_ENV="production", OAOS_MOCK_FALLBACK=None, OAOS_EG_URL="http://127.0.0.1:59999")
    try:
        from agent_runtime.mcp_client import MCPClient
        c = MCPClient(gateway_url="http://127.0.0.1:59999", timeout=1.0)
        with pytest.raises(RuntimeError, match="mock fallback disabled|gateway_unreachable"):
            await c.call_tool("gmail_search", {"query": "hi"})
        # same via proxy_tool_call path
        from execution_gateway.proxy import proxy_tool_call
        ctx = {"trace_id": "trace_test", "request_id": "req_test", "user_id": "employee:test", "tenant_id": "default", "action": "SEARCH", "resource": "gmail/user/*"}
        res = await proxy_tool_call("gmail_search", {"query": "hi"}, None, ctx)
        assert res.get("error") == "MOCK_FALLBACK_DISABLED" or res.get("code") == "MOCK_FALLBACK_DISABLED"
    finally:
        _restore(old)

def test_mock_path_removed_from_prod_image():
    """I-H7-1: prod manifests/compose must NOT contain OAOS_MOCK_FALLBACK; env_gate must be immutable (prod->False unconditionally)."""
    # 1) env_gate files must contain immutable prod guard (is_production() -> False) and must NOT allow OAOS_MOCK_FALLBACK override in prod
    for p in [ROOT / "packages/agent-runtime/agent_runtime/env_gate.py",
              ROOT / "execution-gateway/execution_gateway/env_gate.py",
              ROOT / "control-plane/control_plane/env_gate.py"]:
        assert p.exists(), f"missing {p}"
        txt = p.read_text()
        # must contain immutable guard — prod must return False BEFORE any OAOS_MOCK_FALLBACK override
        assert "if is_production():" in txt and "return False" in txt, f"{p} missing immutable prod gate"
        if p.name != "env_gate.py" or "agent_runtime" in str(p):
            lines = txt.splitlines()
            def_line = next((i for i, l in enumerate(lines) if "def is_mock_allowed" in l), -1)
            sliced = lines[def_line:]
            prod_line = next((i for i, l in enumerate(sliced) if "if is_production()" in l), 9999)
            first_mock_ref = 9999
            for i, l in enumerate(sliced):
                stripped = l.lstrip()
                if stripped.startswith("#"):
                    continue
                if "OAOS_MOCK_FALLBACK" in l:
                    first_mock_ref = i
                    break
            assert prod_line != 9999 and prod_line < first_mock_ref, f"{p} canonical immutable gate violated"
        else:
            assert "from agent_runtime.env_gate import" in txt, f"{p} must delegate to canonical gate"
    # 2) prod deploy manifests must not contain OAOS_MOCK_FALLBACK
    for p in [ROOT / "deploy/docker-compose.prod.yml",
              ROOT / "deploy/k8s/control-plane/deployment.yaml",
              ROOT / "deploy/k8s/execution-gateway/deployment.yaml",
              ROOT / "deploy/k8s/security/deployment.yaml",
              ROOT / "deploy/k8s/configmap.yaml"]:
        if p.exists():
            txt = p.read_text()
            assert "OAOS_MOCK_FALLBACK" not in txt, f"prod manifest {p} must not contain OAOS_MOCK_FALLBACK"
            assert "MOCK_FALLBACK" not in txt, f"prod manifest {p} must not contain MOCK_FALLBACK"
    # 3) providers must delegate to immutable gate (no bypass via direct OAOS_MOCK_FALLBACK in prod)
    for provider in ["claude", "codex", "gemini", "ollama", "openrouter", "opencode_go"]:
        path = ROOT / f"packages/agent-runtime/agent_runtime/providers/{provider}.py"
        if not path.exists():
            continue
        txt = path.read_text()
        # provider _is_mock_allowed should not have unconditional return True for OAOS_MOCK_FALLBACK without prod check
        # At minimum, should contain is_production or is_mock_allowed delegation
        assert "is_mock_allowed" in txt or "is_production" in txt, f"{path} must gate via env_gate"
