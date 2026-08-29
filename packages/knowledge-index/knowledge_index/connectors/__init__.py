"""Connectors package."""

from .base import FetchResult, SourceAdapter, InMemorySourceAdapter

# Real HTTP adapter (read-only sync + gated writes)
try:
    from .http_outline import HttpOutlineSourceAdapter, OutlineAPIError, OutlineSourceConfig

    __all__ = [
        "FetchResult",
        "SourceAdapter",
        "InMemorySourceAdapter",
        "HttpOutlineSourceAdapter",
        "OutlineAPIError",
        "OutlineSourceConfig",
    ]
except Exception:  # pragma: no cover
    __all__ = ["FetchResult", "SourceAdapter", "InMemorySourceAdapter"]
