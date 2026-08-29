"""Execution-gateway environment gate — fail-closed in production.

Mirrored from packages/agent-runtime/agent_runtime/env_gate.py — keep in sync.
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
    mf = os.getenv("OAOS_MOCK_FALLBACK", "").strip().lower()
    if mf in ("1", "true", "yes", "on"):
        return True
    if mf in ("0", "false", "no", "off"):
        return False
    if is_production():
        return False
    return True

def require_real_transport_or_fail(tool_name: str = "") -> None:
    """Fail-closed helper for mock fallback paths in production."""
    if is_production() and not is_mock_allowed():
        raise RuntimeError(f"mock fallback disabled in production for tool={tool_name} (OAOS_ENV=production, OAOS_MOCK_FALLBACK=0 or unset)")

def fail_open_telemetry(component: str, reason: str, **fields) -> None:
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    msg = f"[fail-open] component={component} reason={reason} {extra}".strip()
    logger.warning(msg)
    try:
        import sys
        print(msg, file=sys.stderr)
    except Exception:
        pass
