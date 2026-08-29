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
