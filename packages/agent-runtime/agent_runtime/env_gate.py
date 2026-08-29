"""Centralized environment gate helper — production fail-closed semantics.

Usage:
  from agent_runtime.env_gate import is_production, is_mock_allowed, fail_open_telemetry

Rules (H7 immutable):
  - is_production()  -> True when OAOS_ENV / ENV / OAOS_ENVIRONMENT in {production, prod}
  - is_mock_allowed() -> IMMUTABLE: in production always False (OAOS_MOCK_FALLBACK cannot re-enable mock).
                         In non-production, OAOS_MOCK_FALLBACK=0|false|no|off disables, otherwise True.
                         This makes prod mock paths impossible regardless of env override.
  - fail_open_telemetry() logs WARNING with metric-friendly payload when non-prod fail-open occurs
  - assert_production_mock_gate() / enforce_prod_gate() — startup gate, raises if prod mock would be allowed

This module is the canonical gate; execution-gateway and control-plane vendors copy
the same logic to keep behavior consistent without cross-package imports.
"""
from __future__ import annotations
import logging
import os

logger = logging.getLogger(__name__)

def is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        v = os.getenv(k, "").strip().lower()
        if v in ("production", "prod"):
            return True
    return False

def is_mock_allowed() -> bool:
    # H7 immutable: production never allows mock, regardless of OAOS_MOCK_FALLBACK
    if is_production():
        return False
    # non-prod: allow unless explicitly disabled
    mf = os.getenv("OAOS_MOCK_FALLBACK", "").strip().lower()
    if mf in ("0", "false", "no", "off"):
        return False
    return True

def assert_production_mock_gate() -> None:
    """Startup gate — fail-closed if production mock would be allowed.

    Call at service startup (before any mock fallback).  Raises RuntimeError in prod.
    In non-prod it is a no-op.
    """
    if is_production() and is_mock_allowed():
        raise RuntimeError("H7 immutable gate violated: mock fallback must be disabled in production (OAOS_ENV=production)")

# alias for discoverability
enforce_prod_gate = assert_production_mock_gate

def require_real_transport_or_fail(tool_name: str = "") -> None:
    """Fail-closed helper for mock fallback paths in production."""
    if is_production() and not is_mock_allowed():
        raise RuntimeError(f"mock fallback disabled in production for tool={tool_name} (OAOS_ENV=production)")

def fail_open_telemetry(component: str, reason: str, **fields) -> None:
    """Explicit telemetry for any non-production fail-open path.

    In production this should never be called — callers must fail-closed instead.
    We log at WARNING with structured fields so metrics can alert on fail-open.
    """
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    msg = f"[fail-open] component={component} reason={reason} {extra}".strip()
    logger.warning(msg)
    # also print to stderr for visibility in serverless/test
    try:
        import sys
        print(msg, file=sys.stderr)
    except Exception:
        pass
