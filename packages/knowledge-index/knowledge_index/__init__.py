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

try:
    from .outline_acl import (  # type: ignore
        OutlineACLResolver,
        OutlineACLResolutionError,
        agent_principal_for_email,
    )
except Exception:  # pragma: no cover
    OutlineACLResolver = None  # type: ignore
    OutlineACLResolutionError = None  # type: ignore
    agent_principal_for_email = None  # type: ignore

try:
    from .worker_outline_sync import main as outline_sync_main  # type: ignore
except Exception:  # pragma: no cover
    outline_sync_main = None  # type: ignore

