"""Control-plane shim — re-exports RuntimeRegistry/Router from runtime-adapter package.

Section 16F shim: allows `from control_plane.runtime_router import ...` while
canonical implementation lives in `runtime_adapter`.
"""

from runtime_adapter.registry import RuntimeRegistry, RuntimeEntry  # type: ignore
from runtime_adapter.router import RuntimeRouter  # type: ignore
from runtime_adapter.safe_adapter import SafeRuntimeAdapter, SafeRuntime  # type: ignore

__all__ = ["RuntimeRegistry", "RuntimeEntry", "RuntimeRouter", "SafeRuntimeAdapter", "SafeRuntime"]
