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
]
