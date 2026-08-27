"""open-agent-os-runtime-adapter — public package surface."""

from .adapter import AgentRuntimeAdapter
from .factory import get_adapter, register_adapter, ADAPTER_REGISTRY
from .hermes_adapter import HermesRuntimeAdapter, HermesAdapter

__all__ = [
    "AgentRuntimeAdapter",
    "HermesRuntimeAdapter",
    "HermesAdapter",
    "get_adapter",
    "register_adapter",
    "ADAPTER_REGISTRY",
]
