"""In-memory stores for chunks and checkpoint state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .chunking import Chunk
from .models import SyncCheckpoint


@dataclass
class StoredChunks:
    resource_id: str
    chunks: list[Chunk] = field(default_factory=list)
    embeddings: list[list[float]] = field(default_factory=list)
    content_hash: str = ""
    acl_version: str = ""
    source_updated_at: str = ""
    acl_groups: list[str] = field(default_factory=list)
    acl_users: list[str] = field(default_factory=list)
    source_uri: str = ""
    classification: str = "INTERNAL"
    tenant_id: str = "default"


class InMemoryChunkStore:
    """Ephemeral chunk store — suitable for tests and local dev."""

    def __init__(self) -> None:
        self._store: dict[str, StoredChunks] = {}

    def upsert(
        self,
        resource_id: str,
        chunks: list[Chunk],
        embeddings: list[list[float]],
        content_hash: str = "",
        acl_version: str = "",
        source_updated_at: str = "",
        acl_groups: list[str] | None = None,
        acl_users: list[str] | None = None,
        source_uri: str = "",
        classification: str = "INTERNAL",
        tenant_id: str = "default",
    ) -> None:
        self._store[resource_id] = StoredChunks(
            resource_id=resource_id,
            chunks=list(chunks),
            embeddings=list(embeddings),
            content_hash=content_hash,
            acl_version=acl_version,
            source_updated_at=source_updated_at,
            acl_groups=list(acl_groups or []),
            acl_users=list(acl_users or []),
            source_uri=source_uri,
            classification=classification,
            tenant_id=tenant_id,
        )

    def get(self, resource_id: str) -> StoredChunks | None:
        return self._store.get(resource_id)

    def delete(self, resource_id: str) -> bool:
        return self._store.pop(resource_id, None) is not None

    def list_resource_ids(self) -> set[str]:
        return set(self._store.keys())

    def all_chunks(self) -> list[Chunk]:
        out: list[Chunk] = []
        for v in self._store.values():
            out.extend(v.chunks)
        return out

    def count_chunks(self) -> int:
        return sum(len(v.chunks) for v in self._store.values())


class InMemoryCheckpointStore:
    """In-memory checkpoint persistence."""

    def __init__(self) -> None:
        self._data: dict[str, SyncCheckpoint] = {}

    def load(self, source_system: str) -> SyncCheckpoint | None:
        return self._data.get(source_system)

    def save(self, checkpoint: SyncCheckpoint) -> None:
        self._data[checkpoint.source_system] = checkpoint

    def get_or_create(self, source_system: str) -> SyncCheckpoint:
        cp = self._data.get(source_system)
        if cp is None:
            cp = SyncCheckpoint(source_system=source_system)
            self._data[source_system] = cp
        return cp
