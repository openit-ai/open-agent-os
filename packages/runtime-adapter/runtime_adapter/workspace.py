"""§16A.3.1 Session / User Workspace Isolation — v1.5.1

Namespace: /home/hermes/workspaces/{tenant}/{agent}/{session}/
Path-name-only != isolation — path separation alone is NOT security isolation.
3 levels:
  General      → per-session workspace + process isolation
  Sensitive    → ephemeral sandbox
  High-Risk    → ephemeral container or VM
Retention: delete or safe-retain, no reuse across user/agent/session.
Cross-session deny: Session A cannot read Session B, Agent A cannot read Agent B temp.
"""
from __future__ import annotations

import re
from enum import Enum
from pathlib import Path, PurePosixPath
from dataclasses import dataclass

# ── Constants ──────────────────────────────────────────────────────────

WORKSPACE_ROOT: Path = Path("/home/hermes/workspaces")

class IsolationLevel(str, Enum):
    GENERAL = "general"          # per-session workspace + process isolation
    SENSITIVE = "sensitive"      # ephemeral sandbox
    HIGH_RISK = "high_risk"      # ephemeral container or VM

ISOLATION_LEVELS: dict[str, str] = {
    IsolationLevel.GENERAL.value: "per-session workspace + process isolation",
    IsolationLevel.SENSITIVE.value: "ephemeral sandbox",
    IsolationLevel.HIGH_RISK.value: "ephemeral container or VM",
}

# Retention policies — delete or safe-retain, no reuse
class RetentionPolicy(str, Enum):
    DELETE = "delete"
    SAFE_RETAIN = "safe-retain"

# canonical allowed retention values (normalize underscore/hyphen)
_ALLOWED_RETENTION = {RetentionPolicy.DELETE.value, RetentionPolicy.SAFE_RETAIN.value, "safe_retain"}

# ── Validation helpers ─────────────────────────────────────────────────

# IDs: alphanumeric + hyphen/underscore, 1-64 chars (tenant/agent/session)
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

def _valid_id(value: str) -> bool:
    if not value or not isinstance(value, str):
        return False
    # reject path traversal, slashes, dots abuse
    if "/" in value or "\\" in value or ".." in value:
        return False
    if value in (".", ".."):
        return False
    # allow alphanum + ._-
    return bool(_ID_RE.match(value))

def _normalize_retention(policy: str) -> str:
    return policy.strip().lower().replace("_", "-") if policy else ""

def is_valid_retention(policy: str) -> bool:
    """Retention policy must be 'delete' or 'safe-retain' (no reuse).

    Returns True if policy is valid, False otherwise.
    Accepts 'safe_retain' as alias for 'safe-retain'.
    """
    if not isinstance(policy, str):
        return False
    norm = _normalize_retention(policy)
    return norm in ("delete", "safe-retain")

def check_retention_policy(policy: str) -> bool:
    """Alias for is_valid_retention — retention check helper."""
    return is_valid_retention(policy)

# Compatibility aliases
is_retention_valid = is_valid_retention
validate_retention = is_valid_retention

# ── Isolation level mapping ────────────────────────────────────────────

_ISOLATION_MAP: dict[str, IsolationLevel] = {
    # general → per-session workspace + process isolation
    "general": IsolationLevel.GENERAL,
    "low": IsolationLevel.GENERAL,
    "normal": IsolationLevel.GENERAL,
    "default": IsolationLevel.GENERAL,
    "standard": IsolationLevel.GENERAL,
    # sensitive → ephemeral sandbox
    "sensitive": IsolationLevel.SENSITIVE,
    "confidential": IsolationLevel.SENSITIVE,
    "medium": IsolationLevel.SENSITIVE,
    "moderate": IsolationLevel.SENSITIVE,
    "private": IsolationLevel.SENSITIVE,
    # high-risk → ephemeral container or VM
    "high": IsolationLevel.HIGH_RISK,
    "high-risk": IsolationLevel.HIGH_RISK,
    "high_risk": IsolationLevel.HIGH_RISK,
    "critical": IsolationLevel.HIGH_RISK,
    "highrisk": IsolationLevel.HIGH_RISK,
    "ephemeral_container": IsolationLevel.HIGH_RISK,
    "vm": IsolationLevel.HIGH_RISK,
    "container": IsolationLevel.HIGH_RISK,
}

def isolation_level(task_risk: str) -> IsolationLevel:
    """Map task risk string to IsolationLevel enum.

    Args:
        task_risk: e.g. 'general', 'sensitive', 'high-risk', 'confidential', 'critical'
    Returns:
        IsolationLevel enum
    Raises:
        ValueError if unknown risk level
    """
    if not isinstance(task_risk, str) or not task_risk.strip():
        raise ValueError(f"unknown task_risk: {task_risk!r}")
    key = task_risk.strip().lower().replace(" ", "-").replace("_", "-")
    # normalize double hyphens
    key = re.sub(r"-+", "-", key)
    # try direct lookup
    if key in _ISOLATION_MAP:
        return _ISOLATION_MAP[key]
    # also try underscore variant
    ukey = key.replace("-", "_")
    if ukey in _ISOLATION_MAP:
        return _ISOLATION_MAP[ukey]
    # try original lower
    orig = task_risk.strip().lower()
    if orig in _ISOLATION_MAP:
        return _ISOLATION_MAP[orig]
    raise ValueError(f"unknown task_risk: {task_risk!r} — expected one of {sorted(_ISOLATION_MAP)}")

# also expose as function alias
get_isolation_level = isolation_level

# ── WorkspaceResolver ──────────────────────────────────────────────────

@dataclass(frozen=True)
class WorkspaceContext:
    tenant: str
    agent: str
    session: str

class WorkspaceResolver:
    """Resolve and validate /home/hermes/workspaces/{tenant}/{agent}/{session}/ namespace.

    Path-name-only != isolation: path separation alone is NOT isolation.
    Real isolation requires per-session workspace + process/sandbox/container.
    """

    ROOT: Path = WORKSPACE_ROOT

    def resolve(self, tenant: str, agent: str, session: str) -> Path:
        """Build workspace path: /home/hermes/workspaces/{tenant}/{agent}/{session}

        Validates IDs to prevent traversal.
        """
        for name, val in (("tenant", tenant), ("agent", agent), ("session", session)):
            if not _valid_id(val):
                raise ValueError(f"invalid {name} id: {val!r}")
        return self.ROOT / tenant / agent / session

    # convenience alias
    def workspace_path(self, tenant: str, agent: str, session: str) -> Path:
        return self.resolve(tenant, agent, session)

    def validate_namespace(self, path: str | Path, context: dict | WorkspaceContext) -> bool:
        """Validate that path belongs to the given context namespace.

        Args:
            path: workspace path to validate (string or Path)
            context: dict with keys tenant/agent/session (also accepts tenant_id/agent_id/session_id)
                     or WorkspaceContext
        Returns:
            True if path is within the expected namespace, False otherwise.
        """
        try:
            # Extract expected ids from context
            if isinstance(context, WorkspaceContext):
                tenant = context.tenant
                agent = context.agent
                session = context.session
            elif isinstance(context, dict):
                tenant = context.get("tenant") or context.get("tenant_id") or ""
                agent = context.get("agent") or context.get("agent_id") or ""
                session = context.get("session") or context.get("session_id") or ""
            else:
                # object with attributes
                tenant = getattr(context, "tenant", "") or getattr(context, "tenant_id", "")
                agent = getattr(context, "agent", "") or getattr(context, "agent_id", "")
                session = getattr(context, "session", "") or getattr(context, "session_id", "")
                tenant = str(tenant) if tenant else ""
                agent = str(agent) if agent else ""
                session = str(session) if session else ""

            if not tenant or not agent or not session:
                return False
            if not _valid_id(tenant) or not _valid_id(agent) or not _valid_id(session):
                return False

            expected = self.ROOT / tenant / agent / session
            # Normalize path — PurePosixPath for string handling, then Path
            p = Path(str(path))

            # Reject paths with traversal before resolution
            raw = str(path)
            if ".." in PurePosixPath(raw).parts:
                return False

            # Must be under expected prefix (exact or subpath)
            # Use posix string comparison to avoid symlink issues in tests
            expected_str = expected.as_posix().rstrip("/") + "/"
            p_str = p.as_posix().rstrip("/") + "/"
            # Check exact match or child
            if p_str == expected_str or p_str.startswith(expected_str):
                return True
            # Also handle case where p is parent — must be deny
            return False
        except Exception:
            return False

    def is_retention_valid(self, policy: str) -> bool:
        return is_valid_retention(policy)

    def parse_workspace_path(self, path: str | Path) -> dict | None:
        """Parse /home/hermes/workspaces/{tenant}/{agent}/{session}[/...] into components.

        Returns dict with tenant, agent, session, remainder or None if not under ROOT.
        """
        try:
            p = Path(str(path))
            root_str = self.ROOT.as_posix()
            p_str = p.as_posix()
            if not p_str.startswith(root_str + "/"):
                return None
            rel = p_str[len(root_str) + 1:]  # strip root + "/"
            parts = [x for x in rel.split("/") if x]
            if len(parts) < 3:
                return None
            tenant, agent, session = parts[0], parts[1], parts[2]
            remainder = "/".join(parts[3:]) if len(parts) > 3 else ""
            if not _valid_id(tenant) or not _valid_id(agent) or not _valid_id(session):
                return None
            return {"tenant": tenant, "agent": agent, "session": session, "remainder": remainder}
        except Exception:
            return None


# ── Cross-session deny checker ─────────────────────────────────────────

def is_workspace_access_allowed(requester_context: dict | WorkspaceContext, target_path: str | Path) -> bool:
    """Cross-session deny checker: DENY if tenant/agent/session mismatch.

    Rules (§16A.3.1):
      - Session A cannot read Session B workspace
      - Agent A cannot read Agent B temp files
      - Tenant mismatch → DENY
      - Path outside WORKSPACE_ROOT → DENY
      - No reuse across session/agent/tenant

    Args:
        requester_context: dict with tenant/agent/session (or tenant_id/agent_id/session_id)
                          or WorkspaceContext
        target_path: path being accessed
    Returns:
        True if allowed (same tenant+agent+session and under ROOT), False if DENY
    """
    resolver = WorkspaceResolver()
    parsed = resolver.parse_workspace_path(target_path)
    if parsed is None:
        return False

    # Extract requester ids
    if isinstance(requester_context, WorkspaceContext):
        req_tenant = requester_context.tenant
        req_agent = requester_context.agent
        req_session = requester_context.session
    elif isinstance(requester_context, dict):
        req_tenant = requester_context.get("tenant") or requester_context.get("tenant_id") or ""
        req_agent = requester_context.get("agent") or requester_context.get("agent_id") or ""
        req_session = requester_context.get("session") or requester_context.get("session_id") or ""
    else:
        req_tenant = getattr(requester_context, "tenant", "") or getattr(requester_context, "tenant_id", "")
        req_agent = getattr(requester_context, "agent", "") or getattr(requester_context, "agent_id", "")
        req_session = getattr(requester_context, "session", "") or getattr(requester_context, "session_id", "")
        req_tenant = str(req_tenant) if req_tenant else ""
        req_agent = str(req_agent) if req_agent else ""
        req_session = str(req_session) if req_session else ""

    # Empty requester → DENY
    if not req_tenant or not req_agent or not req_session:
        return False

    # Strict equality on all three dimensions
    if parsed["tenant"] != req_tenant:
        return False
    if parsed["agent"] != req_agent:
        return False
    if parsed["session"] != req_session:
        return False
    return True


# ── Module-level convenience functions ─────────────────────────────────

_default_resolver = WorkspaceResolver()

def resolve_workspace(tenant: str, agent: str, session: str) -> Path:
    return _default_resolver.resolve(tenant, agent, session)

def validate_namespace(path: str | Path, context: dict | WorkspaceContext) -> bool:
    return _default_resolver.validate_namespace(path, context)

def parse_workspace_path(path: str | Path) -> dict | None:
    return _default_resolver.parse_workspace_path(path)

__all__ = [
    "WORKSPACE_ROOT",
    "IsolationLevel",
    "ISOLATION_LEVELS",
    "RetentionPolicy",
    "WorkspaceResolver",
    "WorkspaceContext",
    "isolation_level",
    "get_isolation_level",
    "is_valid_retention",
    "check_retention_policy",
    "is_retention_valid",
    "validate_retention",
    "is_workspace_access_allowed",
    "resolve_workspace",
    "validate_namespace",
    "parse_workspace_path",
]
