"""Tests — §16A.3.1 Session/User Workspace Isolation (v1.5.1)

- namespace format
- cross-session deny (A→B denied, A→A allowed)
- isolation level mapping
- tenant mismatch deny
- additional: agent mismatch, retention policy, validate_namespace, path traversal, no reuse
"""
from __future__ import annotations

import pytest
from pathlib import Path

from runtime_adapter.workspace import (
    WORKSPACE_ROOT,
    ISOLATION_LEVELS,
    IsolationLevel,
    WorkspaceResolver,
    WorkspaceContext,
    isolation_level,
    is_valid_retention,
    check_retention_policy,
    is_workspace_access_allowed,
    validate_namespace,
    parse_workspace_path,
    resolve_workspace,
)
from runtime_adapter.security_notes import (
    WORKSPACE_ROOT as SN_WORKSPACE_ROOT,
    ISOLATION_LEVELS as SN_ISOLATION_LEVELS,
)

# ── Constants exposed via security_notes ───────────────────────────────

def test_security_notes_exposes_workspace_root():
    assert str(SN_WORKSPACE_ROOT) == "/home/hermes/workspaces"
    assert str(WORKSPACE_ROOT) == "/home/hermes/workspaces"

def test_security_notes_exposes_isolation_levels():
    assert "general" in SN_ISOLATION_LEVELS
    assert "sensitive" in SN_ISOLATION_LEVELS
    assert "high_risk" in SN_ISOLATION_LEVELS
    assert SN_ISOLATION_LEVELS == ISOLATION_LEVELS

# ── Namespace format ───────────────────────────────────────────────────

def test_namespace_format_basic():
    r = WorkspaceResolver()
    p = r.resolve("tenant1", "agent1", "session1")
    assert p == Path("/home/hermes/workspaces/tenant1/agent1/session1")
    assert p.as_posix() == "/home/hermes/workspaces/tenant1/agent1/session1"

def test_namespace_format_via_helper():
    p = resolve_workspace("acme", "hermes-a", "sess-123")
    assert str(p) == "/home/hermes/workspaces/acme/hermes-a/sess-123"

def test_namespace_rejects_traversal():
    r = WorkspaceResolver()
    with pytest.raises(ValueError):
        r.resolve("../etc", "agent1", "sess1")
    with pytest.raises(ValueError):
        r.resolve("tenant1", "agent/../evil", "sess1")
    with pytest.raises(ValueError):
        r.resolve("tenant1", "agent1", "..")

def test_namespace_rejects_empty():
    r = WorkspaceResolver()
    with pytest.raises(ValueError):
        r.resolve("", "agent1", "sess1")
    with pytest.raises(ValueError):
        r.resolve("tenant1", "", "sess1")

def test_workspace_root_constant():
    assert WORKSPACE_ROOT == Path("/home/hermes/workspaces")
    assert WORKSPACE_ROOT.as_posix() == "/home/hermes/workspaces"

def test_parse_workspace_path():
    p = parse_workspace_path("/home/hermes/workspaces/t1/a1/s1/file.txt")
    assert p is not None
    assert p["tenant"] == "t1"
    assert p["agent"] == "a1"
    assert p["session"] == "s1"
    assert p["remainder"] == "file.txt"

def test_parse_workspace_path_nested():
    p = parse_workspace_path("/home/hermes/workspaces/tenant-x/agent-y/session-z/a/b/c.py")
    assert p == {"tenant": "tenant-x", "agent": "agent-y", "session": "session-z", "remainder": "a/b/c.py"}

def test_parse_workspace_path_outside_root_none():
    assert parse_workspace_path("/tmp/evil/file") is None
    assert parse_workspace_path("/home/hermes/other/file") is None

def test_parse_workspace_path_incomplete_none():
    assert parse_workspace_path("/home/hermes/workspaces/t1/a1") is None
    assert parse_workspace_path("/home/hermes/workspaces/t1") is None

# ── validate_namespace ─────────────────────────────────────────────────

def test_validate_namespace_allowed():
    r = WorkspaceResolver()
    ctx = {"tenant": "t1", "agent": "a1", "session": "s1"}
    assert r.validate_namespace("/home/hermes/workspaces/t1/a1/s1", ctx) is True
    assert r.validate_namespace("/home/hermes/workspaces/t1/a1/s1/file.txt", ctx) is True
    assert r.validate_namespace("/home/hermes/workspaces/t1/a1/s1/sub/dir/data.json", ctx) is True

def test_validate_namespace_denied_wrong_session():
    r = WorkspaceResolver()
    ctx = {"tenant": "t1", "agent": "a1", "session": "s1"}
    assert r.validate_namespace("/home/hermes/workspaces/t1/a1/s2", ctx) is False
    assert r.validate_namespace("/home/hermes/workspaces/t1/a1/s2/file.txt", ctx) is False

def test_validate_namespace_denied_wrong_agent():
    r = WorkspaceResolver()
    ctx = {"tenant": "t1", "agent": "a1", "session": "s1"}
    assert r.validate_namespace("/home/hermes/workspaces/t1/a2/s1", ctx) is False

def test_validate_namespace_denied_wrong_tenant():
    r = WorkspaceResolver()
    ctx = {"tenant": "t1", "agent": "a1", "session": "s1"}
    assert r.validate_namespace("/home/hermes/workspaces/t2/a1/s1", ctx) is False

def test_validate_namespace_alias_keys():
    r = WorkspaceResolver()
    ctx = {"tenant_id": "t1", "agent_id": "a1", "session_id": "s1"}
    assert r.validate_namespace("/home/hermes/workspaces/t1/a1/s1/file.txt", ctx) is True

def test_validate_namespace_traversal_denied():
    r = WorkspaceResolver()
    ctx = {"tenant": "t1", "agent": "a1", "session": "s1"}
    assert r.validate_namespace("/home/hermes/workspaces/t1/a1/s1/../s2/file", ctx) is False

def test_validate_namespace_outside_root_denied():
    r = WorkspaceResolver()
    ctx = {"tenant": "t1", "agent": "a1", "session": "s1"}
    assert r.validate_namespace("/etc/passwd", ctx) is False
    assert r.validate_namespace("/home/hermes/workspaces_other/t1/a1/s1", ctx) is False

def test_validate_namespace_dataclass_context():
    r = WorkspaceResolver()
    ctx = WorkspaceContext(tenant="t1", agent="a1", session="s1")
    assert r.validate_namespace("/home/hermes/workspaces/t1/a1/s1", ctx) is True
    assert r.validate_namespace("/home/hermes/workspaces/t1/a1/s2", ctx) is False

def test_module_level_validate_namespace():
    ctx = {"tenant": "t1", "agent": "a1", "session": "s1"}
    assert validate_namespace("/home/hermes/workspaces/t1/a1/s1", ctx) is True
    assert validate_namespace("/home/hermes/workspaces/t1/a1/s2", ctx) is False

# ── Cross-session deny ─────────────────────────────────────────────────

def test_cross_session_deny_A_to_B_denied():
    ctx_a = {"tenant": "acme", "agent": "agent-a", "session": "sess-A"}
    target_b = "/home/hermes/workspaces/acme/agent-a/sess-B/file.txt"
    assert is_workspace_access_allowed(ctx_a, target_b) is False

def test_cross_session_deny_A_to_A_allowed():
    ctx_a = {"tenant": "acme", "agent": "agent-a", "session": "sess-A"}
    target_a = "/home/hermes/workspaces/acme/agent-a/sess-A/file.txt"
    assert is_workspace_access_allowed(ctx_a, target_a) is True
    # exact workspace dir itself
    assert is_workspace_access_allowed(ctx_a, "/home/hermes/workspaces/acme/agent-a/sess-A") is True

def test_tenant_mismatch_deny():
    ctx = {"tenant": "tenant-A", "agent": "agent1", "session": "sess1"}
    target = "/home/hermes/workspaces/tenant-B/agent1/sess1/file.txt"
    assert is_workspace_access_allowed(ctx, target) is False

def test_agent_mismatch_deny():
    ctx = {"tenant": "acme", "agent": "agent-A", "session": "sess-1"}
    target = "/home/hermes/workspaces/acme/agent-B/sess-1/file.txt"
    assert is_workspace_access_allowed(ctx, target) is False

def test_cross_session_all_dimensions_must_match():
    ctx = {"tenant": "t1", "agent": "a1", "session": "s1"}
    # same tenant+agent, different session → deny
    assert is_workspace_access_allowed(ctx, "/home/hermes/workspaces/t1/a1/s2") is False
    # same tenant+session, different agent → deny (Agent A cannot read Agent B temp)
    assert is_workspace_access_allowed(ctx, "/home/hermes/workspaces/t1/a2/s1") is False
    # same agent+session, different tenant → deny
    assert is_workspace_access_allowed(ctx, "/home/hermes/workspaces/t2/a1/s1") is False

def test_cross_session_outside_root_denied():
    ctx = {"tenant": "t1", "agent": "a1", "session": "s1"}
    assert is_workspace_access_allowed(ctx, "/tmp/evil") is False
    assert is_workspace_access_allowed(ctx, "/home/hermes/evil") is False

def test_cross_session_empty_context_denied():
    assert is_workspace_access_allowed({}, "/home/hermes/workspaces/t1/a1/s1") is False
    assert is_workspace_access_allowed({"tenant": "t1"}, "/home/hermes/workspaces/t1/a1/s1") is False

def test_cross_session_alias_keys():
    ctx = {"tenant_id": "t1", "agent_id": "a1", "session_id": "s1"}
    assert is_workspace_access_allowed(ctx, "/home/hermes/workspaces/t1/a1/s1/file") is True
    assert is_workspace_access_allowed(ctx, "/home/hermes/workspaces/t1/a1/s2/file") is False

def test_cross_session_dataclass_context():
    ctx = WorkspaceContext(tenant="t1", agent="a1", session="s1")
    assert is_workspace_access_allowed(ctx, "/home/hermes/workspaces/t1/a1/s1") is True
    assert is_workspace_access_allowed(ctx, "/home/hermes/workspaces/t1/a1/s2") is False

# ── Isolation level mapping ────────────────────────────────────────────

def test_isolation_level_general():
    assert isolation_level("general") == IsolationLevel.GENERAL
    assert isolation_level("low") == IsolationLevel.GENERAL
    assert isolation_level("normal") == IsolationLevel.GENERAL
    assert isolation_level("default") == IsolationLevel.GENERAL
    assert isolation_level("GENERAL") == IsolationLevel.GENERAL

def test_isolation_level_sensitive():
    assert isolation_level("sensitive") == IsolationLevel.SENSITIVE
    assert isolation_level("confidential") == IsolationLevel.SENSITIVE
    assert isolation_level("medium") == IsolationLevel.SENSITIVE
    assert isolation_level("moderate") == IsolationLevel.SENSITIVE

def test_isolation_level_high_risk():
    assert isolation_level("high-risk") == IsolationLevel.HIGH_RISK
    assert isolation_level("high_risk") == IsolationLevel.HIGH_RISK
    assert isolation_level("high") == IsolationLevel.HIGH_RISK
    assert isolation_level("critical") == IsolationLevel.HIGH_RISK
    assert isolation_level("HIGH-RISK") == IsolationLevel.HIGH_RISK

def test_isolation_level_unknown_raises():
    with pytest.raises(ValueError):
        isolation_level("unknown-level")
    with pytest.raises(ValueError):
        isolation_level("")
    with pytest.raises(ValueError):
        isolation_level("   ")

def test_isolation_levels_constant():
    assert ISOLATION_LEVELS["general"] == "per-session workspace + process isolation"
    assert ISOLATION_LEVELS["sensitive"] == "ephemeral sandbox"
    assert ISOLATION_LEVELS["high_risk"] == "ephemeral container or VM"

def test_isolation_level_enum_values():
    assert IsolationLevel.GENERAL.value == "general"
    assert IsolationLevel.SENSITIVE.value == "sensitive"
    assert IsolationLevel.HIGH_RISK.value == "high_risk"

# ── Retention policy ───────────────────────────────────────────────────

def test_retention_delete_valid():
    assert is_valid_retention("delete") is True
    assert check_retention_policy("delete") is True

def test_retention_safe_retain_valid():
    assert is_valid_retention("safe-retain") is True
    assert is_valid_retention("safe_retain") is True
    assert check_retention_policy("safe-retain") is True

def test_retention_invalid():
    assert is_valid_retention("reuse") is False
    assert is_valid_retention("keep") is False
    assert is_valid_retention("") is False
    assert is_valid_retention("DELETE ") is True  # case-insensitive, trimmed → valid
    # but unknown values denied
    assert is_valid_retention("permanent") is False

def test_retention_no_reuse_principle():
    # Only delete or safe-retain are valid — no reuse is encoded by denial of other values
    for invalid in ["reuse", "share", "reassign", "recycle", "persist"]:
        assert is_valid_retention(invalid) is False, f"{invalid} should be invalid (no reuse)"

def test_retention_via_resolver():
    r = WorkspaceResolver()
    assert r.is_retention_valid("delete") is True
    assert r.is_retention_valid("safe-retain") is True
    assert r.is_retention_valid("reuse") is False

# ── Path-name != isolation (doc test) ──────────────────────────────────

def test_path_name_only_not_isolation():
    """Path separation alone is not security isolation — cross-session must still DENY."""
    ctx_a = {"tenant": "t1", "agent": "a1", "session": "sess-A"}
    # Even though path is under correct tenant, wrong session → DENY (not just path naming)
    assert is_workspace_access_allowed(ctx_a, "/home/hermes/workspaces/t1/a1/sess-B/secret.txt") is False
    # validate_namespace also denies
    r = WorkspaceResolver()
    assert r.validate_namespace("/home/hermes/workspaces/t1/a1/sess-B/secret.txt", ctx_a) is False

# ── Edge: session isolation even with shared pool ──────────────────────

def test_shared_pool_still_isolated():
    """Shared Hermes Worker Pool still enforces Session A → Session B DENY."""
    tenants = ["acme", "acme"]
    agents = ["agent-1", "agent-1"]
    sessions = ["sess-001", "sess-002"]
    ctx1 = {"tenant": tenants[0], "agent": agents[0], "session": sessions[0]}
    ctx2 = {"tenant": tenants[1], "agent": agents[1], "session": sessions[1]}
    path1 = f"/home/hermes/workspaces/{tenants[0]}/{agents[0]}/{sessions[0]}/data.json"
    path2 = f"/home/hermes/workspaces/{tenants[1]}/{agents[1]}/{sessions[1]}/data.json"
    # each can access own
    assert is_workspace_access_allowed(ctx1, path1) is True
    assert is_workspace_access_allowed(ctx2, path2) is True
    # cross denied
    assert is_workspace_access_allowed(ctx1, path2) is False
    assert is_workspace_access_allowed(ctx2, path1) is False
