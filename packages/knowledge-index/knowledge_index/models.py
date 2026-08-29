"""Data models for knowledge index sync + persistent index (v1.7.1 §0.4.1).

Sync models (SourceDocument etc.) from chunking branch preserved; KnowledgeIndexEntry
added for persistent KnowledgeIndexORM so `from knowledge_index.models import KnowledgeIndexEntry`
works for persistence tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from .chunking import content_hash


class KnowledgeIndexEntry(BaseModel):
    index_id: str = Field(..., min_length=1, max_length=64)
    source_system: str = Field(..., min_length=1, max_length=64)
    source_resource_id: str = Field(..., min_length=1)
    source_uri: str | None = None
    tenant_id: str = Field(..., min_length=1)
    group_id: str | None = None
    agent_id: str | None = None
    chunk_id: str = Field(..., min_length=1)
    chunk_text: str = Field(..., min_length=1)
    embedding: list[float] | None = None
    content_hash: str | None = None
    source_updated_at: datetime | None = None
    indexed_at: datetime | None = None
    acl_version: str | None = None
    classification: str | None = None
    retention_policy: str | None = None
    provenance: dict[str, Any] | None = None

    def to_orm_kwargs(self) -> dict[str, Any]:
        return self.model_dump()


@dataclass
class SourceDocument:
    """Normalized document from a source system (Outline/Notion)."""

    resource_id: str  # e.g. "outline/team/doc_001"
    source_system: str  # "outline" | "notion"
    title: str
    content: str
    source_updated_at: str  # ISO8601 string; used for incremental check
    acl_version: str = "v1"
    acl: dict[str, Any] = field(default_factory=dict)
    source_uri: str = ""
    tenant_id: str = "default"
    classification: str = "INTERNAL"
    content_hash: str = ""  # deterministic SHA256(content)

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = content_hash(self.content)
        if not self.source_uri:
            self.source_uri = self.resource_id
        if not self.source_updated_at:
            self.source_updated_at = datetime.now(timezone.utc).isoformat()


@dataclass
class ResourceState:
    """Checkpointed state for a single resource."""

    content_hash: str
    source_updated_at: str
    acl_version: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ResourceState":
        return cls(
            content_hash=d.get("content_hash", ""),
            source_updated_at=d.get("source_updated_at", ""),
            acl_version=d.get("acl_version", "v1"),
        )


@dataclass
class SyncCheckpoint:
    """Persisted checkpoint for a source sync."""

    source_system: str
    last_sync_at: str = ""
    resource_states: dict[str, ResourceState] = field(default_factory=dict)
    cursor: str | None = None  # opaque pagination/next token

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_system": self.source_system,
            "last_sync_at": self.last_sync_at,
            "cursor": self.cursor,
            "resource_states": {k: v.to_dict() for k, v in self.resource_states.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SyncCheckpoint":
        states: dict[str, ResourceState] = {}
        for k, v in (d.get("resource_states") or {}).items():
            try:
                states[k] = ResourceState.from_dict(v)
            except Exception:
                continue
        return cls(
            source_system=d.get("source_system", ""),
            last_sync_at=d.get("last_sync_at", ""),
            cursor=d.get("cursor"),
            resource_states=states,
        )

    def is_unchanged(self, doc: SourceDocument) -> bool:
        st = self.resource_states.get(doc.resource_id)
        if st is None:
            return False
        return (
            st.content_hash == doc.content_hash
            and st.source_updated_at == doc.source_updated_at
            and st.acl_version == doc.acl_version
        )
