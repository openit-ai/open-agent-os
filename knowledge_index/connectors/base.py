"""Source adapter base + in-memory fixture adapter."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..models import SourceDocument


@dataclass
class FetchResult:
    documents: list[SourceDocument] = field(default_factory=list)
    deleted_resource_ids: list[str] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class SourceAdapter(ABC):
    source_system: str = "unknown"

    @abstractmethod
    def fetch(self, checkpoint: Any | None = None) -> FetchResult: ...


class InMemorySourceAdapter(SourceAdapter):
    """In-memory adapter for tests/fixtures.

    - Holds dict resource_id -> SourceDocument
    - Supports incremental via checkpoint (filters by presence — caller decides unchanged)
    - Supports deleted ids tracking
    - Can inject transient failures for retry tests
    """

    def __init__(
        self,
        source_system: str = "outline",
        documents: list[SourceDocument] | None = None,
        fail_times: int = 0,
    ) -> None:
        self.source_system = source_system
        self._docs: dict[str, SourceDocument] = {}
        for d in documents or []:
            self._docs[d.resource_id] = d
        self._fail_times = int(fail_times)
        self._fetch_calls = 0
        self._deleted: set[str] = set()

    @property
    def fetch_calls(self) -> int:
        return self._fetch_calls

    def set_documents(self, docs: list[SourceDocument]) -> None:
        # track deletions: previously present but now missing
        new_ids = {d.resource_id for d in docs}
        old_ids = set(self._docs.keys())
        self._deleted = old_ids - new_ids
        self._docs = {d.resource_id: d for d in docs}

    def upsert_document(self, doc: SourceDocument) -> None:
        if doc.resource_id in self._deleted:
            self._deleted.discard(doc.resource_id)
        self._docs[doc.resource_id] = doc

    def delete_document(self, resource_id: str) -> None:
        self._docs.pop(resource_id, None)
        self._deleted.add(resource_id)

    def fetch(self, checkpoint: Any | None = None) -> FetchResult:
        self._fetch_calls += 1
        if self._fetch_calls <= self._fail_times:
            raise RuntimeError(f"transient fetch failure {self._fetch_calls}/{self._fail_times}")
        # checkpoint is ignored for in-memory — return all current docs
        # Real adapters would use checkpoint.cursor/last_sync_at to filter
        return FetchResult(
            documents=list(self._docs.values()),
            deleted_resource_ids=list(self._deleted),
        )

    def clear_deleted(self) -> None:
        self._deleted.clear()
