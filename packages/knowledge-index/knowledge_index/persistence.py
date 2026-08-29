"""Enterprise Knowledge Index — package shim (re-exports from root knowledge_index for import compat)."""
from knowledge_index.orm import KnowledgeIndexORM  # type: ignore
from knowledge_index.models import KnowledgeIndexEntry  # type: ignore
from knowledge_index.repository import KnowledgeIndexRepository  # type: ignore
from knowledge_index.retrieval import KnowledgeIndexRetriever, RetrievalHit  # type: ignore

__all__ = ["KnowledgeIndexORM", "KnowledgeIndexEntry", "KnowledgeIndexRepository", "KnowledgeIndexRetriever", "RetrievalHit"]
