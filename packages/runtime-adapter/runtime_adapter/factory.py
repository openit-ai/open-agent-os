"""Factory & registry for runtime adapters — Section 16E."""

from __future__ import annotations

from typing import Callable, Dict

from .adapter import AgentRuntimeAdapter

ADAPTER_REGISTRY: Dict[str, Callable[[], AgentRuntimeAdapter]] = {}


def register_adapter(name: str, factory: Callable[[], AgentRuntimeAdapter]) -> None:
    """Register a new adapter factory under ``name`` (case-insensitive)."""
    ADAPTER_REGISTRY[name.lower()] = factory


def get_adapter(name: str | None = None, **kwargs) -> AgentRuntimeAdapter:
    """Return an adapter instance by name.

    Args:
        name: Adapter name. ``None`` or ``"hermes"`` returns the Hermes adapter
              (default per Section 16E — hermes is the initial/primary runtime).
        **kwargs: Forwarded to the adapter constructor (e.g. ``hermes_base_url``).

    Raises:
        ValueError: If the requested adapter is not registered.

    Example:
        adapter = get_adapter()  # hermes default
        adapter = get_adapter("hermes", hermes_base_url="http://localhost:8001")
    """
    key = (name or "hermes").lower()
    if key not in ADAPTER_REGISTRY:
        # Lazy import to avoid circular import at module load
        if key == "hermes":
            from .hermes_adapter import HermesRuntimeAdapter

            def _hermes_factory() -> AgentRuntimeAdapter:
                return HermesRuntimeAdapter(**kwargs) if kwargs else HermesRuntimeAdapter()

            ADAPTER_REGISTRY[key] = _hermes_factory
        elif key == "safe":
            from .safe_adapter import SafeRuntimeAdapter

            def _safe_factory() -> AgentRuntimeAdapter:
                return SafeRuntimeAdapter(**kwargs) if kwargs else SafeRuntimeAdapter()

            ADAPTER_REGISTRY[key] = _safe_factory
        else:
            available = ", ".join(sorted(ADAPTER_REGISTRY.keys())) or "(none)"
            raise ValueError(f"Unknown runtime adapter '{name}'. Available: {available}")

    factory = ADAPTER_REGISTRY[key]
    # If caller passed kwargs and factory is the default hermes/safe one, re-invoke with kwargs
    if kwargs and key in ("hermes", "safe"):
        if key == "hermes":
            from .hermes_adapter import HermesRuntimeAdapter

            return HermesRuntimeAdapter(**kwargs)
        else:
            from .safe_adapter import SafeRuntimeAdapter

            return SafeRuntimeAdapter(**kwargs)
    return factory()


# Eagerly register hermes + safe so ADAPTER_REGISTRY is populated on import
try:
    from .hermes_adapter import HermesRuntimeAdapter as _Hermes

    ADAPTER_REGISTRY.setdefault("hermes", lambda: _Hermes())
except Exception:  # pragma: no cover
    pass
try:
    from .safe_adapter import SafeRuntimeAdapter as _Safe

    ADAPTER_REGISTRY.setdefault("safe", lambda: _Safe())
except Exception:  # pragma: no cover
    pass
