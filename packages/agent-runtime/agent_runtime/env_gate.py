"""Centralized environment gate helper — production fail-closed semantics.

Usage:
  from agent_runtime.env_gate import is_production, is_mock_allowed, fail_open_telemetry

Rules:
  - is_production()  -> True when OAOS_ENV / ENV / OAOS_ENVIRONMENT in {production, prod}
  - is_mock_allowed() -> explicit OAOS_MOCK_FALLBACK overrides; in production defaults to False (fail-closed)
  - fail_open_telemetry() logs WARNING with metric-friendly payload when non-prod fail-open occurs

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
    mf = os.getenv("OAOS_MOCK_FALLBACK", "").strip().lower()
    if mf in ("1", "true", "yes", "on"):
        return True
    if mf in ("0", "false", "no", "off"):
        return False
    # no explicit override -> production => mock disabled
    if is_production():
        return False
    return True

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
