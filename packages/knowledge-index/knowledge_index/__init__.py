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

# Re-export RAG service wrappers (no import side-effects if service missing)
try:
    from .service import (  # type: ignore
        KnowledgeSearchService,
        KnowledgeSyncService,
        KnowledgeMaterializationService,
        OutlineSyncService,
        OutlineMaterializationService,
        KnowledgeIndexService,
        search_knowledge,
        sync_outline_to_index,
        materialize_knowledge_to_outline,
        SyncServiceConfig,
    )
except Exception:  # pragma: no cover
    KnowledgeSearchService = None  # type: ignore
    KnowledgeSyncService = None  # type: ignore
    KnowledgeMaterializationService = None  # type: ignore
    OutlineSyncService = None  # type: ignore
    OutlineMaterializationService = None  # type: ignore
    KnowledgeIndexService = None  # type: ignore
    search_knowledge = None  # type: ignore
    sync_outline_to_index = None  # type: ignore
    materialize_knowledge_to_outline = None  # type: ignore
    SyncServiceConfig = None  # type: ignore

