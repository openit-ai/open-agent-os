"""Control-plane environment gate — canonical import shim.

The single source of truth is ``agent_runtime.env_gate``. Keeping this module
as a shim prevents policy drift between Control Plane and package consumers.
"""
from agent_runtime.env_gate import (  # type: ignore
    assert_production_mock_gate,
    enforce_prod_gate,
    fail_open_telemetry,
    is_mock_allowed,
    is_production,
)
