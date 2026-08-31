"""Notion source adapters.

- NotionSourceAdapter: in-memory fixture (no live API) for tests — preserved.
- HttpNotionSourceAdapter: real read-only Notion HTTP adapter (see http_notion.py).
"""

from __future__ import annotations

from datetime import datetime, timezone
from ..chunking import content_hash
from ..models import SourceDocument
from .base import InMemorySourceAdapter

# Re-export real HTTP adapter
try:
    from .http_notion import (
        HttpNotionSourceAdapter,
        NotionAPIError,
        NotionSourceConfig,
    )
except Exception:  # pragma: no cover - http_notion missing on minimal installs
    HttpNotionSourceAdapter = None  # type: ignore
    NotionAPIError = RuntimeError  # type: ignore
    NotionSourceConfig = None  # type: ignore


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_notion_doc(
    page_id: str,
    database: str = "wiki",
    title: str = "Untitled",
    content: str = "",
    acl_version: str = "v1",
    acl: dict | None = None,
    updated_at: str | None = None,
) -> SourceDocument:
    rid = f"notion/{database}/{page_id}"
    return SourceDocument(
        resource_id=rid,
        source_system="notion",
        title=title,
        content=content,
        source_updated_at=updated_at or _iso_now(),
        acl_version=acl_version,
        acl=acl or {},
        source_uri=f"https://notion.example.com/{page_id}",
        content_hash=content_hash(content),
    )


class NotionSourceAdapter(InMemorySourceAdapter):
    def __init__(self, documents: list[SourceDocument] | None = None, fail_times: int = 0) -> None:
        super().__init__(source_system="notion", documents=documents, fail_times=fail_times)
