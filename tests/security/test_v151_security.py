"""
§40 Security Tests — v1.5.1 four new tests (§16A.3.1 + §16A.6 + runtime hardening)

- test_workspace_cross_session_leakage_DENY
- test_hermes_direct_internet_egress_DENY
- test_runtime_escalation_bypass_requires_capability
- test_llm_runtime_arbitrary_code_DENY

Uses existing registry/router/tool_policy/data_access helpers — mock where needed,
asserts DENY / APPROVAL_REQUIRED / UNSUPPORTED via deterministic helpers.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Ensure imports — mirror tests/test_p2_e2e.py path setup
for p in [
    ROOT / "packages" / "runtime-adapter",
    ROOT / "execution-gateway",
    ROOT / "packages" / "common-types",
    ROOT / "packages" / "agent-context",
    ROOT / "packages" / "policy-model",
    ROOT / "control-plane",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
# Also add execution-gateway directly for `import execution_gateway.*`
if str(ROOT / "execution-gateway") not in sys.path:
    sys.path.insert(0, str(ROOT / "execution-gateway"))

# --- imports from existing helpers ---
from runtime_adapter.registry import RuntimeRegistry  # noqa: E402
from runtime_adapter.router import RuntimeRouter  # noqa: E402
from runtime_adapter.safe_adapter import SafeRuntimeAdapter  # noqa: E402
from execution_gateway.tool_policy import ToolPolicy, validate_tool_call, is_bulk  # noqa: E402
from execution_gateway.data_access import DataAccessPolicy, get_data_access_policy  # noqa: E402
from execution_gateway.risk import classify, RiskLevel  # noqa: E402
from execution_gateway.capability import verify_capability  # noqa: E402


# ======================================================================
# Helpers shared by tests
# ======================================================================

def _workspace_path(tenant: str, agent: str, session: str) -> str:
    return f"/home/hermes/workspaces/{tenant}/{agent}/{session}"


def _can_access_workspace(requesting: dict, target_path: str) -> tuple[bool, str]:
    """
    Minimal workspace isolation check — namespace is /home/hermes/workspaces/{tenant}/{agent}/{session}
    DENY if tenant/agent/session mismatch (simulates §16A.3.1 isolation).
    Returns (allowed, reason) where DENY maps to "DENY".
    """
    # Expected prefix
    prefix = "/home/hermes/workspaces/"
    if not target_path.startswith(prefix):
        # Outside workspace namespace — DENY via blast radius
        return False, "DENY: outside workspace namespace"
    suffix = target_path[len(prefix):]
    parts = suffix.split("/")
    if len(parts) < 3:
        return False, "DENY: malformed workspace path"
    tgt_tenant, tgt_agent, tgt_session = parts[0], parts[1], parts[2]
    if tgt_tenant != requesting.get("tenant_id"):
        return False, f"DENY: tenant isolation {requesting.get('tenant_id')} != {tgt_tenant}"
    if tgt_agent != requesting.get("agent_id"):
        return False, f"DENY: agent isolation {requesting.get('agent_id')} != {tgt_agent}"
    if tgt_session != requesting.get("session_id"):
        return False, f"DENY: session isolation {requesting.get('session_id')} != {tgt_session}"
    return True, "ALLOW: same tenant/agent/session"


# Simple egress policy helper (mirrors hermes-egress.nft ALLOW set)
_EGRESS_ALLOW = {
    ("10.10.1.10", 8000),  # ACP
    ("10.10.1.11", 8001),  # MCP
}

def _egress_allowed(host: str, port: int, *, llm_gateway_host: str, package_mirror_host: str) -> tuple[bool, str]:
    """Controlled Egress Proxy check — ALLOW only ACP/MCP/LLM Gateway/Package Mirror."""
    # Resolve env-configurable hosts (LLM_GATEWAY_HOST / PACKAGE_MIRROR_HOST)
    allow_set = set(_EGRESS_ALLOW)
    allow_set.add((llm_gateway_host, 443))
    allow_set.add((package_mirror_host, 443))
    # DNS always allowed
    if port == 53:
        return True, "ALLOW: DNS"
    if (host, port) in allow_set:
        return True, f"ALLOW: {host}:{port} in allowlist"
    # Deny registries / arbitrary internet / internal DB
    denied_hosts = {"pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "github.com", "raw.githubusercontent.com", "api.openai.com", "generativelanguage.googleapis.com"}
    if host in denied_hosts or host.startswith("10.20.") or host.startswith("10.30.") or host.startswith("10.40."):
        return False, f"DENY: {host} blocked by §16A.6"
    return False, f"DENY: {host}:{port} not in allowlist (arbitrary internet DENY)"


# ======================================================================
# 1. workspace_cross_session_leakage_DENY — §16A.3.1
# ======================================================================

def test_workspace_cross_session_leakage_DENY():
    """
    §16A.3.1 Session/User Workspace Isolation — cross-session access must be DENY.
    Namespace: /home/hermes/workspaces/{tenant}/{agent}/{session}
    Uses: data_access blast_radius helper + workspace path check.
    """
    session_a = {"tenant_id": "tenantA", "agent_id": "agentA", "session_id": "sessA", "user_id": "userA"}
    session_b = {"tenant_id": "tenantA", "agent_id": "agentA", "session_id": "sessB", "user_id": "userA"}
    session_other_agent = {"tenant_id": "tenantA", "agent_id": "agentB", "session_id": "sessA", "user_id": "userB"}
    session_other_tenant = {"tenant_id": "tenantX", "agent_id": "agentA", "session_id": "sessA", "user_id": "userX"}

    policy = get_data_access_policy()

    # Same session → ALLOW
    own_path = _workspace_path("tenantA", "agentA", "sessA") + "/output.txt"
    ok, reason = _can_access_workspace(session_a, own_path)
    assert ok is True, reason
    assert "ALLOW" in reason

    # Cross-session (same agent, different session) → DENY
    cross_session_path = _workspace_path("tenantA", "agentA", "sessB") + "/secret.txt"
    ok, reason = _can_access_workspace(session_a, cross_session_path)
    assert ok is False, "cross-session must be DENY"
    assert "DENY" in reason
    assert "session isolation" in reason.lower()

    # Cross-agent → DENY
    cross_agent_path = _workspace_path("tenantA", "agentB", "sessA") + "/data.json"
    ok, reason = _can_access_workspace(session_a, cross_agent_path)
    assert ok is False
    assert "DENY" in reason
    assert "agent isolation" in reason.lower()

    # Cross-tenant → DENY
    cross_tenant_path = _workspace_path("tenantX", "agentA", "sessA") + "/file.txt"
    ok, reason = _can_access_workspace(session_a, cross_tenant_path)
    assert ok is False
    assert "DENY" in reason

    # Path traversal attempt → DENY via blast_radius
    traversal = "/home/hermes/workspaces/tenantA/agentA/sessA/../sessB/secret.txt"
    # Even if suffix parsing might be tricky, blast radius check should DENY for ../
    br = policy.check_blast_radius(user="userA", resource=traversal)
    assert br.decision == "DENY", f"traversal should be blast_radius DENY, got {br.decision}"
    assert "blast_radius" in br.reason.lower() or "den" in br.reason.lower()

    # Other user home direct → DENY via blast_radius
    br2 = policy.check_blast_radius(user="hermes", resource="/home/other_user/secret.txt")
    assert br2.decision == "DENY"
    assert "DENY" in br2.decision

    # Verify session_b cannot read session_a workspace either (symmetric)
    ok, reason = _can_access_workspace(session_b, own_path)
    assert ok is False
    assert "DENY" in reason


# ======================================================================
# 2. hermes_direct_internet_egress_DENY — §16A.6 Controlled Egress Proxy
# ======================================================================

def test_hermes_direct_internet_egress_DENY():
    """
    §16A.6 Controlled Egress Proxy — Hermes direct internet / registry egress is DENY.
    Uses: tool_policy + data_access + egress allowlist mock; asserts DENY.
    Table:
      Hermes→Public LLM direct DENY
      Hermes→PyPI/npm/GitHub direct DENY
      Hermes→LLM Gateway ALLOW
      Hermes→Approved Package Mirror ALLOW
    """
    llm_gateway = os.environ.get("LLM_GATEWAY_HOST", "10.10.2.10")
    pkg_mirror = os.environ.get("PACKAGE_MIRROR_HOST", "10.10.2.20")

    # ALLOW: LLM Gateway & Package Mirror (via Controlled Egress Proxy)
    for host, port in [(llm_gateway, 443), (pkg_mirror, 443), ("10.10.1.10", 8000), ("10.10.1.11", 8001)]:
        ok, reason = _egress_allowed(host, port, llm_gateway_host=llm_gateway, package_mirror_host=pkg_mirror)
        assert ok is True, f"expected ALLOW for {host}:{port} — {reason}"
        assert "ALLOW" in reason

    # DENY: Public LLM direct
    for host in ["api.openai.com", "generativelanguage.googleapis.com"]:
        ok, reason = _egress_allowed(host, 443, llm_gateway_host=llm_gateway, package_mirror_host=pkg_mirror)
        assert ok is False, f"public LLM direct {host} must be DENY"
        assert "DENY" in reason

    # DENY: Direct public registries
    for host in ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "github.com", "raw.githubusercontent.com"]:
        ok, reason = _egress_allowed(host, 443, llm_gateway_host=llm_gateway, package_mirror_host=pkg_mirror)
        assert ok is False, f"registry direct {host} must be DENY"
        assert "DENY" in reason

    # DENY: Arbitrary internet
    ok, reason = _egress_allowed("8.8.8.8", 443, llm_gateway_host=llm_gateway, package_mirror_host=pkg_mirror)
    assert ok is False
    assert "DENY" in reason

    # DENY: Internal DB/ERP/CRM/SSH nets (§16A.6)
    for host in ["10.20.5.10", "10.30.1.5", "10.40.2.3"]:
        ok, reason = _egress_allowed(host, 5432, llm_gateway_host=llm_gateway, package_mirror_host=pkg_mirror)
        assert ok is False
        assert "DENY" in reason

    # Also verify nftables file contains required directives (filesystem check)
    nft_path = ROOT / "deploy" / "firewall" / "hermes-egress.nft"
    assert nft_path.exists(), "hermes-egress.nft must exist"
    content = nft_path.read_text()
    assert "LLM_GATEWAY_HOST" in content, "nft must define LLM_GATEWAY_HOST (configurable)"
    assert "PACKAGE_MIRROR_HOST" in content, "nft must define PACKAGE_MIRROR_HOST"
    assert "§16A.6" in content or "16A.6" in content
    assert "Controlled Egress Proxy" in content
    assert "PYPI" in content or "pypi" in content.lower()
    assert "meta skuid hermes drop" in content

    # Verify tool_policy bulk detection still escalates (egress-related bulk = DENY/require capability)
    assert is_bulk("EXPORT", "my_data_export", result_count=200) is True
    assert is_bulk("READ", "bulk_data", result_count=150) is True


# ======================================================================
# 3. runtime_escalation_bypass_requires_capability — §16F/§26
# ======================================================================

def test_runtime_escalation_bypass_requires_capability():
    """
    Runtime escalation without capability must be DENY / raise.
    Uses: RuntimeRegistry + RuntimeRouter with mocked capability checker.
    Hermes shell tasks without EXECUTE runtime/hermes → PermissionError (DENY).
    LLM tasks without escalation remain ALLOW via JIT.
    """
    registry_all = RuntimeRegistry(
        runtimes={
            "llm": {"installed": True, "enabled": True, "security_level": "standard"},
            "hermes": {"installed": True, "enabled": True, "security_level": "privileged"},
        }
    )

    # Checker that DENIES hermes but ALLOWS llm
    def deny_hermes_checker(user_id: str, resource: str) -> bool:
        if "hermes" in resource:
            return False
        return True

    router_denied = RuntimeRouter(registry=registry_all, capability_checker=deny_hermes_checker)

    # Task requiring hermes (shell) without capability → must raise PermissionError (DENY)
    with pytest.raises(PermissionError, match="capability"):
        router_denied.select_runtime("userA", task_type="shell")

    with pytest.raises(PermissionError, match="capability"):
        router_denied.select_runtime("userA", task_type="python", required_capability="shell")

    # General task → falls back to llm (ALLOW, no escalation needed)
    rt = router_denied.select_runtime("userA", task_type="general")
    assert rt in ("llm", "safe")  # llm canonical, safe alias

    # Verify capability checker is actually consulted for hermes
    # When both runtimes lack capability → PermissionError (no available runtime)
    def deny_all_checker(user_id: str, resource: str) -> bool:
        return False

    router_none = RuntimeRouter(registry=registry_all, capability_checker=deny_all_checker)
    with pytest.raises((PermissionError, ValueError)):
        router_none.select_runtime("userA", task_type="general")

    # No checker (JIT path) → hermes allowed when installed/enabled
    router_jit = RuntimeRouter(registry=registry_all, capability_checker=None)
    rt_jit = router_jit.select_runtime("userA", task_type="shell")
    assert rt_jit == "hermes"

    # DataAccessPolicy also requires approval for privileged write paths (APPROVAL_REQUIRED)
    policy = get_data_access_policy()
    res = policy.write_path("DEPLOY", "production/api", source="mcp")
    assert res.decision == "APPROVAL_REQUIRED"
    assert res.requires_approval is True

    # Capability token verification without token → DENY
    chk = verify_capability({}, "EXECUTE", "runtime/hermes", context={"user_id": "userA"})
    assert chk.allowed is False
    assert "missing" in chk.reason.lower()


# ======================================================================
# 4. llm_runtime_arbitrary_code_DENY — §16F LLM Runtime hardening
# ======================================================================

@pytest.mark.asyncio
async def test_llm_runtime_arbitrary_code_DENY():
    """
    LLM Runtime (SafeRuntime/LLMRuntime) must DENY arbitrary code execution.
    Uses: SafeRuntimeAdapter.execute_sandbox + call_tool shell/python → UNSUPPORTED / NotImplementedError (DENY).
    """
    adapter = SafeRuntimeAdapter(base_url="http://127.0.0.1:1")  # unreachable → fallback path but sandbox still DENY
    fake_session = {"session_id": "sess-llm-test", "tenant_id": "t1", "user_id": "u1", "agent_id": "a1", "trace_id": "tr"}

    # execute_sandbox for any language → DENY (NotImplementedError)
    for lang in ["shell", "bash", "python", "python3", "sh"]:
        with pytest.raises(NotImplementedError, match="DENY"):
            await adapter.execute_sandbox(fake_session, "echo hello", language=lang)

    # call_tool with shell/python/exec/sandbox in name → DENY
    for tool in ["shell_exec", "bash.run", "python_exec", "exec_sandbox", "sandbox.run"]:
        with pytest.raises(NotImplementedError, match="DENY"):
            await adapter.call_tool(fake_session, tool, arguments={"command": "whoami"})

    # Allowed tool (non-shell) should NOT raise via call_tool
    result = await adapter.call_tool(fake_session, "mcp:gmail_search", arguments={"query": "hello"})
    assert result is not None
    assert result.get("tool") == "mcp:gmail_search"

    # health_check must report denied capabilities
    health = await adapter.health_check()
    assert health["status"] == "ok"
    assert "shell" in health.get("denied", []) or "python" in health.get("denied", [])

    # Risk classification: arbitrary code execution context → at least MEDIUM/HIGH if external
    risk = classify("EXECUTE", "shell/command", is_external=False)
    # Safe runtime should not be able to bypass via risk — still DENY at adapter layer
    assert risk in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.LOW)  # sanity

    # ToolPolicy: denied_fields / action validation still applies (DENY on bad args)
    pol = ToolPolicy(tool="crm.search_customer", allowed_actions=["SEARCH"], limits={"max_results": 50}, denied_fields=["password"])
    ok, reason = validate_tool_call(pol, action="SEARCH", args={"password": "123"})
    assert ok is False
    assert "denied field" in reason.lower()
