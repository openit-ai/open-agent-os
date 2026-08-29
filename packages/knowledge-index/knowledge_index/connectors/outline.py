"""Outline source adapter fixture (no live API)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..chunking import content_hash
from ..models import SourceDocument
from .base import FetchResult, InMemorySourceAdapter


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_outline_doc(
    doc_id: str,
    collection: str = "team",
    title: str = "Untitled",
    content: str = "",
    acl_version: str = "v1",
    acl: dict | None = None,
    updated_at: str | None = None,
) -> SourceDocument:
    rid = f"outline/{collection}/{doc_id}"
    return SourceDocument(
        resource_id=rid,
        source_system="outline",
        title=title,
        content=content,
        source_updated_at=updated_at or _iso_now(),
        acl_version=acl_version,
        acl=acl or {},
        source_uri=f"https://outline.example.com/doc/{doc_id}",
        content_hash=content_hash(content),
    )


class OutlineSourceAdapter(InMemorySourceAdapter):
    """Outline in-memory adapter with ACL metadata."""

    def __init__(self, documents: list[SourceDocument] | None = None, fail_times: int = 0) -> None:
        super().__init__(source_system="outline", documents=documents, fail_times=fail_times)
