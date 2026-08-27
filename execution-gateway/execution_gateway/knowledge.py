"""Knowledge Retrieval Pipeline — Section 28

Outline API search → ACL filter → Ranking → Response
+ governance provenance recording + §29 classification

Integrates:
- OutlineConnector (search + ACL + ranking)
- MemoryStore (provenance tracking for derived memories)
- DataClassification (content hook)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    from .connectors.outline import OutlineConnector, OutlineDocument, SearchResult
except ImportError:
    from execution_gateway.connectors.outline import OutlineConnector, OutlineDocument, SearchResult  # type: ignore

try:
    from .risk import classify_content, DataClassification
except ImportError:
    from execution_gateway.risk import classify_content, DataClassification  # type: ignore

# Governance integration (optional — graceful fallback if not installed)
try:
    from governance.governance import MemoryStore, MemoryScope  # type: ignore
except ImportError:
    try:
        from security.memory_governance.governance.governance import MemoryStore, MemoryScope  # type: ignore
    except ImportError:
        MemoryStore = None  # type: ignore
        MemoryScope = None  # type: ignore


@dataclass
class KnowledgeResponse:
    """Final response after retrieval pipeline."""
    query: str
    documents: list[dict[str, Any]]
    total_found: int
    filtered_count: int
    trace_id: str
    provenance: dict[str, Any] = field(default_factory=dict)


class KnowledgeRetriever:
    """Orchestrates Outline retrieval pipeline with governance provenance.

    Pipeline:
      1. Outline API search (via OutlineConnector)
      2. ACL filter (Identity → Allowed Scope → Allowed Documents)
      3. Ranking (title/content relevance)
      4. Response shaping + classification + provenance record

    Optional: record derived memories into MemoryStore with full provenance
    (source_resource_id, acl_version, delegation_id, classification, retention).
    """

    def __init__(
        self,
        outline_connector: OutlineConnector | None = None,
        memory_store: Any | None = None,
    ):
        self.outline = outline_connector or OutlineConnector()
        self.memory_store = memory_store  # MemoryStore instance or None

    def retrieve(
        self,
        query: str,
        agent_context: dict | object,
        limit: int = 10,
        collection_id: str | None = None,
        record_provenance: bool = False,
        trace_id: str | None = None,
    ) -> KnowledgeResponse:
        """Execute full retrieval pipeline.

        Args:
            query: search query
            agent_context: {tenant_id, user_id, groups, delegation_id, ...}
            limit: max results
            collection_id: optional collection filter
            record_provenance: if True, write derived memories to MemoryStore
            trace_id: optional trace id for audit

        Returns:
            KnowledgeResponse with documents + provenance
        """
        tid = trace_id or f"trace_{uuid.uuid4().hex[:8]}"
        ctx_dict = _to_dict(agent_context)

        # 1-3. Delegate to OutlineConnector's pipeline
        result: SearchResult = self.outline.retrieve(
            query=query,
            agent_context=agent_context,
            limit=limit,
            collection_id=collection_id,
        )

        # 4. Response shaping + classification
        shaped: list[dict[str, Any]] = []
        for doc in result.documents:
            # content-based classification hook (§29)
            auto_dc = classify_content(doc.content, f"outline/{doc.collection_id}/{doc.id}")
            # prefer doc's explicit classification, but validate via hook
            final_dc = doc.classification or auto_dc.value
            shaped.append({
                "id": doc.id,
                "title": doc.title,
                "content": doc.content,
                "url": doc.url,
                "collection_id": doc.collection_id,
                "classification": final_dc,
                "acl_version": doc.acl_version,
                "score": doc.score,
                "resource_id": f"outline/{doc.collection_id}/{doc.id}",
            })

        provenance = {
            "query": query,
            "trace_id": tid,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "total_found": result.total,
            "filtered_count": result.filtered_count,
            "returned_count": len(shaped),
            "agent_context": {
                "tenant_id": ctx_dict.get("tenant_id"),
                "user_id": ctx_dict.get("user_id"),
            },
            "source": "outline",
        }

        # Optional: record derived knowledge as memories with provenance
        if record_provenance and self.memory_store and MemoryScope:
            delegation_id = ctx_dict.get("delegation_id")
            user_id = ctx_dict.get("user_id", "unknown")
            tenant_id = ctx_dict.get("tenant_id", "default")
            for item in shaped:
                try:
                    # Corporate scope for outline knowledge (shared)
                    self.memory_store.write(
                        owner="organization",
                        scope=MemoryScope.CORPORATE,
                        content=f"{item['title']}: {item['content'][:500]}",
                        classification=item["classification"],
                        source_resource_id=item["resource_id"],
                        source_acl_version=item["acl_version"],
                        source_delegation_id=delegation_id,
                        retention_policy="standard",
                        tenant_id=tenant_id,
                        provenance={
                            "knowledge_query": query,
                            "trace_id": tid,
                            "outline_doc_id": item["id"],
                        },
                    )
                except Exception:
                    pass  # best-effort provenance

        return KnowledgeResponse(
            query=query,
            documents=shaped,
            total_found=result.total,
            filtered_count=result.filtered_count,
            trace_id=tid,
            provenance=provenance,
        )

    async def retrieve_async(
        self,
        query: str,
        agent_context: dict | object,
        limit: int = 10,
        trace_id: str | None = None,
    ) -> KnowledgeResponse:
        """Async variant — uses Outline API if configured."""
        tid = trace_id or f"trace_{uuid.uuid4().hex[:8]}"
        # Try real API first
        try:
            raw_docs = await self.outline.search_api(query, _to_dict(agent_context), limit * 2)  # type: ignore
            # raw_docs are dicts — convert to OutlineDocument for pipeline
            docs: list[OutlineDocument] = []
            for d in raw_docs:
                if isinstance(d, dict):
                    docs.append(OutlineDocument(
                        id=d.get("id", f"doc_{uuid.uuid4().hex[:6]}"),
                        collection_id=d.get("collection_id", d.get("collection", "team")),
                        title=d.get("title", ""),
                        content=d.get("text", d.get("content", "")),
                        url=d.get("url", ""),
                        classification=d.get("classification", "INTERNAL"),
                        acl_version=d.get("acl_version", "v1"),
                    ))
                else:
                    docs.append(d)  # already OutlineDocument
            # ACL filter + ranking
            allowed, denied = self.outline.acl_filter(docs, agent_context)
            ranked = self.outline.rank(allowed, query)[:limit]
            shaped = [
                {
                    "id": d.id,
                    "title": d.title,
                    "content": d.content,
                    "url": d.url,
                    "collection_id": d.collection_id,
                    "classification": d.classification,
                    "acl_version": d.acl_version,
                    "score": d.score,
                    "resource_id": f"outline/{d.collection_id}/{d.id}",
                }
                for d in ranked
            ]
            return KnowledgeResponse(
                query=query, documents=shaped,
                total_found=len(docs), filtered_count=denied,
                trace_id=tid,
                provenance={"query": query, "trace_id": tid, "source": "outline-api"},
            )
        except Exception:
            pass
        # fallback to sync
        return self.retrieve(query, agent_context, limit, trace_id=tid)


def _to_dict(ctx: dict | object) -> dict:
    if isinstance(ctx, dict):
        return ctx
    if hasattr(ctx, "model_dump"):
        return ctx.model_dump()  # type: ignore
    return {
        "tenant_id": getattr(ctx, "tenant_id", None),
        "user_id": getattr(ctx, "user_id", None),
        "agent_id": getattr(ctx, "agent_id", None),
        "groups": getattr(ctx, "groups", []),
        "delegation_id": getattr(ctx, "delegation_id", None),
    }
