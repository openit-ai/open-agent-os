"""Enterprise Knowledge Index — ACL + persistent index (v1.7.1 §0.4).

This package is the importable `knowledge_index` (ROOT/knowledge_index).
Persistent ORM/repository/retrieval live here; chunking/embedding sync lives in
packages/knowledge-index/knowledge_index.
"""
from .acl import (
    ACLPolicy,
    ChunkRecord,
    InvalidationResult,
    KnowledgeACLIndex,
    RevalidationResult,
    SourceState,
)

try:
    from .orm import KnowledgeIndexORM  # type: ignore
    from .models import KnowledgeIndexEntry  # type: ignore
    from .repository import KnowledgeIndexRepository  # type: ignore
    from .retrieval import KnowledgeIndexRetriever, RetrievalHit  # type: ignore
except Exception:  # pragma: no cover
    KnowledgeIndexORM = None  # type: ignore
    KnowledgeIndexEntry = None  # type: ignore
    KnowledgeIndexRepository = None  # type: ignore
    KnowledgeIndexRetriever = None  # type: ignore
    RetrievalHit = None  # type: ignore

__all__ = [
    "ACLPolicy",
    "ChunkRecord",
    "InvalidationResult",
    "KnowledgeACLIndex",
    "RevalidationResult",
    "SourceState",
    "KnowledgeIndexORM",
    "KnowledgeIndexEntry",
    "KnowledgeIndexRepository",
    "KnowledgeIndexRetriever",
    "RetrievalHit",
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

