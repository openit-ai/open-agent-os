"""Re-export shim — canonical implementation lives in ``runtime_adapter`` package (§16E).

This module preserves ``control_plane.hermes_adapter`` as the import path for
existing code/tests while delegating to the new ``open-agent-os-runtime-adapter``
package.
"""

from __future__ import annotations

# Re-export the concrete adapter and its alias for backward compatibility
from runtime_adapter.hermes_adapter import HermesAdapter, HermesRuntimeAdapter  # noqa: F401
from runtime_adapter.adapter import AgentRuntimeAdapter  # noqa: F401
from runtime_adapter.factory import get_adapter, ADAPTER_REGISTRY, register_adapter  # noqa: F401

__all__ = [
    "HermesAdapter",
    "HermesRuntimeAdapter",
    "AgentRuntimeAdapter",
    "get_adapter",
    "ADAPTER_REGISTRY",
    "register_adapter",
]
