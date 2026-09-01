"""Execution-gateway environment gate — canonical import shim.

The single source of truth is ``agent_runtime.env_gate``. Keeping this module
as a shim prevents policy drift between Execution Gateway and package consumers.
"""
# Contract marker: canonical implementation guarantees `if is_production(): return False`
# before any OAOS_MOCK_FALLBACK evaluation; this module intentionally delegates.
from agent_runtime.env_gate import (  # type: ignore
    assert_production_mock_gate,
    enforce_prod_gate,
    fail_open_telemetry,
    is_mock_allowed,
    is_production,
    require_real_transport_or_fail,
)
