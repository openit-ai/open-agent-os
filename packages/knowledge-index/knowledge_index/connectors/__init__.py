"""Connectors package."""

from .base import FetchResult, SourceAdapter, InMemorySourceAdapter

# Real HTTP adapters (read-only sync + gated writes)
try:
    from .http_outline import HttpOutlineSourceAdapter, OutlineAPIError, OutlineSourceConfig

    _has_outline = True
except Exception:  # pragma: no cover
    HttpOutlineSourceAdapter = None  # type: ignore
    OutlineAPIError = RuntimeError  # type: ignore
    OutlineSourceConfig = None  # type: ignore
    _has_outline = False

try:
    from .http_notion import HttpNotionSourceAdapter, NotionAPIError, NotionSourceConfig

    _has_notion = True
except Exception:  # pragma: no cover
    HttpNotionSourceAdapter = None  # type: ignore
    NotionAPIError = RuntimeError  # type: ignore
    NotionSourceConfig = None  # type: ignore
    _has_notion = False

__all__ = ["FetchResult", "SourceAdapter", "InMemorySourceAdapter"]
if _has_outline:
    __all__ += ["HttpOutlineSourceAdapter", "OutlineAPIError", "OutlineSourceConfig"]
if _has_notion:
    __all__ += ["HttpNotionSourceAdapter", "NotionAPIError", "NotionSourceConfig"]
