"""Knowledge Index — chunking, embeddings, connector sync orchestration."""

from .chunking import chunk_text, content_hash, make_chunks, Chunk, ChunkConfig
from .embedding import (
    EmbeddingProvider,
    FakeEmbeddingProvider,
    HashEmbeddingProvider,
    get_default_provider,
)
from .models import SourceDocument, SyncCheckpoint, ResourceState
from .sync import SyncOrchestrator, SyncResult
from .store import InMemoryChunkStore, InMemoryCheckpointStore

from .acl import (
    ACLPolicy,
    ChunkRecord,
    InvalidationResult,
    KnowledgeACLIndex,
    RevalidationResult,
    SourceState,
)

__all__ = [
    "chunk_text",
    "content_hash",
    "make_chunks",
    "Chunk",
    "ChunkConfig",
    "EmbeddingProvider",
    "FakeEmbeddingProvider",
    "HashEmbeddingProvider",
    "get_default_provider",
    "SourceDocument",
    "SyncCheckpoint",
    "ResourceState",
    "SyncOrchestrator",
    "SyncResult",
    "InMemoryChunkStore",
    "InMemoryCheckpointStore",
    "ACLPolicy",
    "ChunkRecord",
    "InvalidationResult",
    "KnowledgeACLIndex",
    "RevalidationResult",
    "SourceState",
]
