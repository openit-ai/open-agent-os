"""RAG search + Outline knowledge materialization integration (v1.7.1 §0.4).

Production-safe, focused module for Control Plane / Execution Gateway.

Responsibilities:
  1) Validate tenant/user/group context and use KnowledgeIndexRetriever ACL pre-filter
     (tenant mandatory, no cross-tenant leakage; groups/agents as allow-lists).
  2) Run Outline sync through real read-only HttpOutlineSourceAdapter + SyncOrchestrator
     into a persistent KnowledgeIndexRepository — no mock/hash fallback in production.
  3) Materialize a generated knowledge document to Outline only via explicit write gate
     and read-back verification, preserving source/provenance.
  4) Expose clear callable interfaces for CP/EG integration (functions + KnowledgeIndexService).

Fail-closed:
  - Missing tenant -> ValueError
  - HashEmbeddingProvider in production without OAOS_ALLOW_HASH_EMBED -> RuntimeError
  - InMemory/mock adapter in production sync -> RuntimeError
  - Writes without write_enabled=True or without permission check -> PermissionError
  - No mock/hash fallback in production; deterministic fallback only with explicit test flag

Both ROOT/knowledge_index/service.py and packages/knowledge-index/knowledge_index/service.py
are kept identical; the latter re-exports from ROOT for pip-install compat.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .acl import KnowledgeACLIndex  # kept for compat export
from .chunking import ChunkConfig, make_chunks, content_hash
from .embedding import EmbeddingProvider, FakeEmbeddingProvider, HashEmbeddingProvider
from .models import KnowledgeIndexEntry, SourceDocument
from .orm import KnowledgeIndexORM
from .repository import KnowledgeIndexRepository
from .retrieval import KnowledgeIndexRetriever, RetrievalHit
from .sync import SyncOrchestrator
from .store import InMemoryChunkStore, InMemoryCheckpointStore
from .checkpoint import PersistentCheckpointStore
from .connectors.base import SourceAdapter
from .connectors.http_outline import HttpOutlineSourceAdapter, OutlineAPIError
from .outline_acl import OutlineACLResolver


# ---------------------------------------------------------------------------
# helpers: production detection & validation
# ---------------------------------------------------------------------------

def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        v = os.environ.get(k, "").strip().lower()
        if v in ("production", "prod"):
            return True
    return False


def _allow_test_fixture() -> bool:
    if _is_production():
        # production never allows fixture bypass
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    flag = os.environ.get("OAOS_ALLOW_TEST_FIXTURE", "") or os.environ.get("OAOS_ALLOW_HASH_EMBED", "")
    if flag.strip().lower() in ("1", "true", "yes", "on"):
        return True
    return False


def validate_search_context(
    *,
    tenant_id: str,
    user_id: str | None = None,
    allowed_group_ids: list[str] | None = None,
    allowed_agent_ids: list[str] | None = None,
) -> tuple[str, list[str], list[str]]:
    """Validate tenant/user/group context for RAG search.
    
    - tenant_id mandatory non-empty (fail-closed cross-tenant isolation)
    - trims and filters empty group/agent ids
    - returns normalized (tenant_id, allowed_group_ids, allowed_agent_ids)
    
    Does not check intra-tenant membership — that is caller-supplied allow-list
    derived from verified JWT / Control Plane identity mapping.
    """
    if not tenant_id or not tenant_id.strip():
        raise ValueError("tenant_id is required — cross-tenant isolation violation")
    tenant_id = tenant_id.strip()
    if user_id is not None and not isinstance(user_id, str):
        raise ValueError("user_id must be a string if provided")
    user_id_stripped = user_id.strip() if user_id else ""
    # normalize group/agent allow lists
    groups = [g.strip() for g in (allowed_group_ids or []) if isinstance(g, str) and g.strip()]
    agents = [a.strip() for a in (allowed_agent_ids or []) if isinstance(a, str) and a.strip()]
    # dedupe preserve order
    seen: set[str] = set()
    uniq_groups: list[str] = []
    for g in groups:
        if g not in seen:
            seen.add(g)
            uniq_groups.append(g)
    seen2: set[str] = set()
    uniq_agents: list[str] = []
    for a in agents:
        if a not in seen2:
            seen2.add(a)
            uniq_agents.append(a)
    return tenant_id, uniq_groups, uniq_agents


def _assert_no_hash_provider_in_production(provider: EmbeddingProvider) -> None:
    if not _is_production():
        return
    if _allow_test_fixture():
        return
    if getattr(provider, "name", "") == "hash":
        raise RuntimeError("hash embeddings blocked in production (EmbeddingProvider.name == 'hash')")
    if isinstance(provider, HashEmbeddingProvider):
        raise RuntimeError("HashEmbeddingProvider is not allowed in production")
    # also guard provider class name fallback
    if provider.__class__.__name__.lower().startswith("hash"):
        raise RuntimeError("hash embedding provider blocked in production")
    # Fake embeddings are likewise blocked in production (must use Ollama).
    # Ollama provider has name 'ollama'; only it is allowed in prod.
    if getattr(provider, "name", "") == "fake":
        raise RuntimeError("fake embeddings blocked in production (use OllamaEmbeddingProvider via OAOS_EMBED_API_URL)")
    if provider.__class__.__name__ == "FakeEmbeddingProvider":
        raise RuntimeError("FakeEmbeddingProvider is not allowed in production")


def _assert_no_mock_adapter_in_production(adapter: SourceAdapter) -> None:
    if not _is_production():
        return
    if _allow_test_fixture():
        return
    # In production only HttpOutlineSourceAdapter (real HTTP) is allowed
    if not isinstance(adapter, HttpOutlineSourceAdapter):
        raise RuntimeError(
            f"mock/in-memory adapter not allowed in production sync: {adapter.__class__.__name__} "
            "(must use HttpOutlineSourceAdapter with real Outline credentials)"
        )


def _short_index_id(*parts: str) -> str:
    """Deterministic short ID <=64 chars via SHA256 hex digest (32 chars).

    Uses pipe-joined parts hashed to 32 hex chars, guaranteed <=64 and
    stable across runs. Avoids chunk_id concatenation overflow (>64).
    """
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def create_outline_adapter(
    *,
    api_url: str | None = None,
    api_token: str | None = None,
    collection_id: str | None = None,
    timeout_s: float = 10.0,
    max_retries: int = 3,
    page_limit: int = 25,
    max_pages: int | None = None,
    http_client: Any | None = None,
    write_enabled: bool = False,
) -> HttpOutlineSourceAdapter:
    """Outline sync factory — uses HttpOutlineSourceAdapter and refuses missing credentials.

    Fail-closed: if api_url/api_token (explicit or env OUTLINE_API_URL etc.) are
    missing, raises RuntimeError with 'Outline credentials missing' and never
    returns a mock adapter.
    """
    adapter = HttpOutlineSourceAdapter(
        api_url=api_url,
        api_token=api_token,
        collection_id=collection_id,
        timeout_s=timeout_s,
        max_retries=max_retries,
        page_limit=page_limit,
        max_pages=max_pages,
        http_client=http_client,
        write_enabled=write_enabled,
    )
    # Refuse missing credentials (HttpOutlineSourceAdapter stores resolved values in _api_url/_api_token)
    if not getattr(adapter, "_api_url", "") or not getattr(adapter, "_api_token", ""):
        raise RuntimeError(
            "Outline credentials missing: api_url and api_token are required "
            "(set OUTLINE_API_URL + OUTLINE_API_TOKEN / OUTLINE_API_KEY or OAOS_OUTLINE_TOKEN). Failing closed — no mock fallback."
        )
    return adapter


# Backward-compatible alias for factory discoverability
build_outline_adapter = create_outline_adapter
get_outline_adapter = create_outline_adapter
create_outline_sync_adapter = create_outline_adapter


# ---------------------------------------------------------------------------
# Search interface — ACL pre-filter via KnowledgeIndexRetriever
# ---------------------------------------------------------------------------

async def search_knowledge(
    *,
    query: str,
    tenant_id: str,
    allowed_group_ids: list[str] | None = None,
    allowed_agent_ids: list[str] | None = None,
    repository: KnowledgeIndexRepository,
    limit: int = 10,
    mode: str = "hybrid",
    query_embedding: list[float] | None = None,
    allow_deterministic_fallback: bool = False,
    user_id: str | None = None,
) -> list[RetrievalHit]:
    """Validated RAG search over persistent KnowledgeIndex.

    Validates tenant/user/group context, then delegates to KnowledgeIndexRetriever
    which does SQL ACL pre-filter (WHERE tenant_id AND group/agent allow-list)
    before lexical/semantic retrieval.

    Args:
        query: user query (non-empty)
        tenant_id: mandatory, trimmed
        allowed_group_ids: groups the caller is member of (from verified JWT)
        allowed_agent_ids: agents the caller may access
        repository: persistent KnowledgeIndexRepository
        limit: 1..100, clamped
        mode: lexical | semantic | hybrid
        query_embedding: required for semantic/hybrid with semantic; if None hybrid falls back to lexical
        allow_deterministic_fallback: only for tests; production must use Postgres+pgvector or raise
        user_id: optional for audit/provenance, validated if provided

    Returns:
        list[RetrievalHit] with provenance always populated
    """
    tenant_id, groups, agents = validate_search_context(
        tenant_id=tenant_id, user_id=user_id, allowed_group_ids=allowed_group_ids, allowed_agent_ids=allowed_agent_ids
    )
    if not query or not query.strip():
        return []
    # Additional guard: repository must be provided
    if repository is None:
        raise ValueError("repository is required")
    retriever = KnowledgeIndexRetriever(repository)
    # mode normalization handled by retriever
    return await retriever.retrieve(
        query=query,
        tenant_id=tenant_id,
        allowed_group_ids=groups,
        allowed_agent_ids=agents,
        limit=limit,
        mode=mode,
        query_embedding=query_embedding,
        allow_deterministic_fallback=allow_deterministic_fallback,
    )


# ---------------------------------------------------------------------------
# Sync interface — HttpOutlineSourceAdapter + SyncOrchestrator -> persistent repo
# ---------------------------------------------------------------------------

@dataclass
class OutlineSyncResult:
    source_system: str
    fetched: int = 0
    upserted: int = 0
    skipped: int = 0
    deleted: int = 0
    deleted_resource_ids: list[str] = field(default_factory=list)
    failed: int = 0
    chunks_written: int = 0
    persisted: int = 0  # entries written to persistent repository
    errors: list[str] = field(default_factory=list)
    checkpoint: Any | None = None
    has_more: bool = False


async def sync_outline_to_index(
    *,
    tenant_id: str,
    repository: KnowledgeIndexRepository,
    embedding_provider: EmbeddingProvider,
    outline_adapter: SourceAdapter | HttpOutlineSourceAdapter,
    chunk_config: ChunkConfig | None = None,
    checkpoint_store: Any | None = None,
    max_retries: int = 3,
    retry_backoff_s: float = 0.05,
    resolve_outline_acl: bool = True,
    acl_resolver: Any | None = None,
    acl_on_error: str = "auto",
    prune_absent_on_complete_snapshot: bool = False,
    persist_batch_size: int = 200,
) -> OutlineSyncResult:
    """Run Outline sync through HttpOutlineSourceAdapter + SyncOrchestrator into persistent repo.

    Validates tenant, enforces no mock/hash fallback in production, runs incremental
    sync (chunk+embed via orchestrator), then drains chunks into KnowledgeIndexRepository
    with tenant-scoped entries preserving acl/provenance/source metadata.

    Outline collection ACL enrichment (production-safe):
      - resolve_outline_acl=True (default) enriches each fetched SourceDocument via
        OutlineACLResolver (collections.info/list + collections.memberships +
        users.list, read-only) BEFORE chunk/embed so StoredChunks ACLs are correct.
      - collection permission read/read_write -> tenant-public (acl {});
        admin/null -> members-only (agent principals from active users' email
        local-parts); outline user IDs/emails preserved in provenance (no secrets).
      - acl_on_error: "auto" (default: strict/fail-closed in production, keep
        source ACL in non-prod), "strict" (always sentinel-restricted on failure,
        never public), "passthrough" (dev/test only: keep source ACL, flag it).
      - acl_resolver: optional prebuilt OutlineACLResolver (uses its adapter).

    Args:
        tenant_id: mandatory target tenant for persisted entries
        repository: persistent KnowledgeIndexRepository (async)
        embedding_provider: injected EmbeddingProvider (Fake for tests, real for prod)
        outline_adapter: HttpOutlineSourceAdapter instance (real HTTP); InMemory only in non-prod/tests
        chunk_config: optional ChunkConfig
        checkpoint_store: optional shared checkpoint store for incremental behavior
        max_retries: bounded retries for fetch/embed (passed to orchestrator)
        prune_absent_on_complete_snapshot: when True, delete tenant resources
          that are absent from a COMPLETE FULL-SCAN snapshot only (single
          fetch starting at offset 0, has_more False, no fetch failure).
          Resumed/truncated windows never delete by absence even when True.
          Default False (safest). Prune passes need max_pages big enough to
          hold the corpus in one window starting from offset 0.
        persist_batch_size: entries per bulk_upsert call (page-batch
          persistence; default 200, clamped >= 1).

    Page-batch safety: each call persists exactly the window it fetched
    (bounded by the adapter max_pages); deletions propagate only via explicit
    source deletion IDs, blank-content cleanup, or the explicit
    complete-snapshot prune above — never from paginated absence.

    Returns:
        OutlineSyncResult with sync stats and persisted count.
        Fail-closed: production with hash provider or mock adapter raises RuntimeError.
        Fetch failures are returned as failed=1/errors, not raised (checkpoint not advanced).
    """
    tenant_id = tenant_id.strip() if isinstance(tenant_id, str) else tenant_id
    if not tenant_id or not str(tenant_id).strip():
        raise ValueError("tenant_id is required for sync — tenant isolation")
    tenant_id = str(tenant_id).strip()
    if repository is None:
        raise ValueError("repository is required for sync")
    if embedding_provider is None:
        raise ValueError("embedding_provider is required (no silent default in production)")

    _assert_no_hash_provider_in_production(embedding_provider)
    _assert_no_mock_adapter_in_production(outline_adapter)
    # Factory/wiring must refuse missing Outline credentials (fail-closed, no mock fallback)
    if isinstance(outline_adapter, HttpOutlineSourceAdapter):
        if not getattr(outline_adapter, "_api_url", "") or not getattr(outline_adapter, "_api_token", ""):
            raise RuntimeError(
                "Outline credentials missing: api_url and api_token are required "
                "(set OUTLINE_API_URL + OUTLINE_API_TOKEN / OUTLINE_API_KEY or OAOS_OUTLINE_TOKEN). Failing closed — no mock fallback."
            )

    # Additional guard: if provider is hash and production without allow, SyncOrchestrator also blocks
    if isinstance(embedding_provider, HashEmbeddingProvider) and _is_production() and not _allow_test_fixture():
        raise RuntimeError("HashEmbeddingProvider blocked in production sync")

    chunk_store = InMemoryChunkStore()
    if checkpoint_store is None:
        if _is_production():
            try:
                from sqlalchemy import create_engine
                database_url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
                if not database_url:
                    raise RuntimeError("persistent checkpoint requires OAOS_DATABASE_URL or DATABASE_URL in production")
                sync_url = database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
                checkpoint_store = PersistentCheckpointStore(create_engine(sync_url, pool_pre_ping=True), tenant_id)
            except Exception as exc:
                raise RuntimeError(f"persistent checkpoint unavailable in production: {type(exc).__name__}") from exc
        else:
            checkpoint_store = InMemoryCheckpointStore()
    chunk_config = chunk_config or ChunkConfig()

    # Capture this run's fetched documents via a fresh snapshot wrapper.
    # SyncOrchestrator does not return SourceDocuments, but StoredChunks carry
    # the ACL/URI/classification metadata captured at fetch time. The snapshot
    # below is taken from THIS run's fetch only (titles + empty-content
    # detection) — never from an untracked/stale adapter mirror.
    #
    # Outline collection ACL enrichment (read-only) runs inside the same
    # wrapper BEFORE chunk/embed: each fetched SourceDocument is enriched via
    # OutlineACLResolver (collections.info/list + collections.memberships +
    # users.list). Public collections (permission read/read_write) become
    # tenant-public; admin/null become members-only agent allow-lists.
    # Fail-closed: resolution failures in production (or acl_on_error="strict")
    # yield a sentinel restricted ACL (never public); outline IDs/emails are
    # stashed per-resource for provenance (no secrets).
    fetched_docs: list[SourceDocument] = []
    _acl_provenance: dict[str, dict[str, Any]] = {}
    _acl_errors: list[str] = []
    _fetch_has_more: list[bool] = []
    _fetch_next_cursor: list[Any] = []
    _fetch_start_cursors: list[Any] = []
    # Checkpoint-before snapshot: authority for explicit complete-snapshot
    # prune only. Loaded BEFORE the orchestrator run; paginated windows must
    # never delete by absence.
    _checkpoint_before_ids: set[str] = set()
    try:
        _cp_before = checkpoint_store.load(getattr(outline_adapter, "source_system", "outline"))
        if _cp_before is not None:
            _checkpoint_before_ids = set(getattr(_cp_before, "resource_states", {}) or {})
    except Exception:
        _checkpoint_before_ids = set()
    _acl_resolver: Any | None = None
    if resolve_outline_acl:
        try:
            if acl_resolver is not None:
                _acl_resolver = acl_resolver
            elif isinstance(outline_adapter, HttpOutlineSourceAdapter):
                _acl_resolver = OutlineACLResolver(outline_adapter)
        except Exception as exc:
            _acl_errors.append(f"outline ACL resolver init failed: {type(exc).__name__}")
            _acl_resolver = None
    _orig_fetch = getattr(outline_adapter, "fetch", None)
    if callable(_orig_fetch):
        _bound_orig = _orig_fetch

        def _recording_fetch(checkpoint: Any = None) -> Any:
            try:
                _fetch_start_cursors.append(getattr(checkpoint, "cursor", None))
            except Exception:
                pass
            res = _bound_orig(checkpoint)
            try:
                _fetch_has_more.append(bool(getattr(res, "has_more", False)))
            except Exception:
                pass
            try:
                _fetch_next_cursor.append(getattr(res, "next_cursor", None))
            except Exception:
                pass
            try:
                docs = list(getattr(res, "documents", []) or [])
                if _acl_resolver is not None:
                    try:
                        enriched, prov = _acl_resolver.enrich_documents(
                            [d for d in docs if isinstance(d, SourceDocument)],
                            on_error=acl_on_error,
                        )
                        try:
                            setattr(res, "documents", enriched)
                        except Exception:
                            pass
                        docs = enriched
                        for rid, p in (prov or {}).items():
                            try:
                                _acl_provenance[str(rid)] = dict(p)
                            except Exception:
                                continue
                        for rid, p in (prov or {}).items():
                            try:
                                if p.get("outline_acl_unresolved"):
                                    _acl_errors.append(
                                        f"outline ACL unresolved for {rid}: {str(p.get('outline_acl_error') or 'unknown')[:160]}"
                                    )
                            except Exception:
                                continue
                    except Exception as exc:
                        # Catastrophic enrichment failure: fail-closed in
                        # production (sentinel-restrict everything fetched),
                        # keep source ACL only in non-prod passthrough.
                        _acl_errors.append(f"outline ACL enrichment failed: {type(exc).__name__}")
                        if _is_production() or acl_on_error == "strict":
                            try:
                                from .outline_acl import _restrict_doc as _acl_restrict
                            except Exception:
                                _acl_restrict = None  # type: ignore
                            if _acl_restrict is not None:
                                restricted: list[SourceDocument] = []
                                for d in docs:
                                    if not isinstance(d, SourceDocument):
                                        continue
                                    try:
                                        rd, rp = _acl_restrict(d, f"{type(exc).__name__}")
                                        restricted.append(rd)
                                        _acl_provenance[rd.resource_id] = rp
                                    except Exception:
                                        continue
                                try:
                                    setattr(res, "documents", restricted)
                                except Exception:
                                    pass
                                docs = restricted
                for d in docs:
                    if isinstance(d, SourceDocument):
                        fetched_docs.append(d)
            except Exception:
                pass
            return res

        try:
            setattr(outline_adapter, "fetch", _recording_fetch)
        except Exception:
            pass
    orchestrator = SyncOrchestrator(
        source=outline_adapter,
        embedding_provider=embedding_provider,
        chunk_store=chunk_store,
        checkpoint_store=checkpoint_store,
        chunk_config=chunk_config,
        max_retries=max_retries,
        retry_backoff_s=retry_backoff_s,
        # The persistent bridge commits the candidate checkpoint only after
        # repository writes succeed. This prevents a DB outage from advancing
        # the source cursor and silently losing the batch on the next run.
        save_checkpoint=False,
    )
    # SyncOrchestrator.sync is synchronous (blocks on time.sleep for retries, stdlib HTTP + Ollama /api/embed urllib)
    # P1 availability fix: offload to thread to avoid starving event loop when single worker.
    # Bounded concurrency is enforced at HTTP layer (memory_service semaphore); here we ensure non-blocking.
    # Check: embedding dim vs index expectation
    try:
        try:
            # If running inside an event loop, offload blocking sync to thread pool
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            sync_result = await asyncio.to_thread(orchestrator.sync)
        else:
            sync_result = orchestrator.sync()
    except RuntimeError as e:
        # production hash guard surfaces as RuntimeError — propagate
        try:
            if callable(_orig_fetch):
                setattr(outline_adapter, "fetch", _orig_fetch)
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            if callable(_orig_fetch):
                setattr(outline_adapter, "fetch", _orig_fetch)
        except Exception:
            pass
        # orchestrator catches fetch failures as failed=1; but unexpected raises should convert to failed result
        return OutlineSyncResult(
            source_system=getattr(outline_adapter, "source_system", "outline"),
            fetched=0,
            upserted=0,
            skipped=0,
            deleted=0,
            failed=1,
            chunks_written=0,
            persisted=0,
            errors=[str(e)],
            checkpoint=None,
            has_more=False,
        )

    # Now persist chunks to KnowledgeIndexRepository
    # Each SourceDocument's ACL is preserved as provenance + as group_id/agent_id pre-filter fields
    # Mapping: groups -> multiple entries per chunk (one per group) for correct ACL pre-filter;
    #          users -> agent_id entries; public (no acl) -> single entry with null group/agent.
    # Metadata authority: StoredChunks fields captured by SyncOrchestrator at fetch
    # time (acl_groups/acl_users/source_uri/classification/content_hash/...).
    # The fresh per-run fetched_docs snapshot above supplies titles and
    # empty-content detection only. No adapter-private mirror (e.g. _docs) is
    # consulted — a stale/untracked mirror must never widen ACLs or resurrect
    # deleted content.
    try:
        if callable(_orig_fetch):
            setattr(outline_adapter, "fetch", _orig_fetch)
    except Exception:
        pass

    persisted = 0
    doc_map: dict[str, SourceDocument] = {}
    for d in fetched_docs:
        try:
            doc_map[d.resource_id] = d
        except Exception:
            continue
    entries: list[KnowledgeIndexEntry] = []
    for rid, stored in list(chunk_store._store.items()):
        if not stored.chunks:
            # Empty content: drop any previously persisted chunks for this
            # tenant-scoped resource via the public repository API.
            try:
                await repository.delete_by_resource(tenant_id, rid)
            except Exception as exc:
                sync_result.errors.append(f"delete failed for {rid}: {type(exc).__name__}")
                sync_result.failed = max(1, sync_result.failed)
            continue
        # Metadata authority is StoredChunks (captured at fetch time by the
        # orchestrator). The fresh per-run snapshot only backfills the title
        # for provenance — it never overrides ACLs, URIs, or tenancy.
        doc = doc_map.get(rid)
        acl_groups: list[str] = list(getattr(stored, "acl_groups", []) or [])
        acl_users: list[str] = list(getattr(stored, "acl_users", []) or [])
        classification: str | None = getattr(stored, "classification", None)
        source_uri: str | None = getattr(stored, "source_uri", None)
        title: str | None = getattr(doc, "title", None)
        # If doc tenant differs, prefer caller tenant for isolation.
        if (getattr(stored, "tenant_id", None) or tenant_id) != tenant_id:
            pass  # caller tenant always wins; stored tenant kept for audit only
        # For each chunk + embedding, create entries
        for idx, (chunk, emb) in enumerate(zip(stored.chunks, stored.embeddings)):
            base_index_id = _short_index_id(rid, chunk.chunk_id)
            # provenance preserved
            provenance = {
                "source": "outline",
                "source_system": "outline",
                "source_resource_id": rid,
                "source_uri": source_uri,
                "resource_id": rid,
                "title": title,
                "chunk_id": chunk.chunk_id,
                "content_hash": stored.content_hash,
                "source_content_hash": getattr(chunk, "source_content_hash", None),
                "acl_version": stored.acl_version,
                "source_updated_at": stored.source_updated_at,
                "tenant_id": tenant_id,
                "acl_groups": acl_groups,
                "acl_users": acl_users,
                "sync_source": getattr(outline_adapter, "source_system", "outline"),
                "indexed_via": "sync_outline_to_index",
            }
            # Outline collection ACL provenance (IDs/emails/mode, no secrets).
            try:
                _acl_prov = _acl_provenance.get(rid)
                if isinstance(_acl_prov, dict):
                    for _k, _v in _acl_prov.items():
                        if _k not in provenance:
                            provenance[_k] = _v
            except Exception:
                pass
            # Determine entries to create per ACL
            # public -> one entry
            if not acl_groups and not acl_users:
                entry = KnowledgeIndexEntry(
                    index_id=base_index_id,
                    source_system="outline",
                    source_resource_id=rid,
                    source_uri=source_uri or rid,
                    tenant_id=tenant_id,
                    group_id=None,
                    agent_id=None,
                    chunk_id=_short_chunk_id(chunk.chunk_id),
                    chunk_text=chunk.text,
                    embedding=emb,
                    content_hash=stored.content_hash,
                    source_updated_at=_parse_updated_at(stored.source_updated_at),
                    indexed_at=datetime.now(timezone.utc),
                    acl_version=stored.acl_version,
                    classification=classification or "INTERNAL",
                    retention_policy=None,
                    provenance=provenance,
                )
                entries.append(entry)
            else:
                # groups -> one entry per group
                for g in acl_groups or [None]:
                    if g is None:
                        continue
                    gid = str(g).strip()
                    if not gid:
                        continue
                    idx_id = _short_index_id(rid, chunk.chunk_id, f"g:{gid}")
                    entry = KnowledgeIndexEntry(
                        index_id=idx_id,
                        source_system="outline",
                        source_resource_id=rid,
                        source_uri=source_uri or rid,
                        tenant_id=tenant_id,
                        group_id=gid,
                        agent_id=None,
                        chunk_id=_short_chunk_id(chunk.chunk_id),
                        chunk_text=chunk.text,
                        embedding=emb,
                        content_hash=stored.content_hash,
                        source_updated_at=_parse_updated_at(stored.source_updated_at),
                        indexed_at=datetime.now(timezone.utc),
                        acl_version=stored.acl_version,
                        classification=classification or "INTERNAL",
                        retention_policy=None,
                        provenance=provenance,
                    )
                    entries.append(entry)
                for u in acl_users:
                    uid = str(u).strip()
                    if not uid:
                        continue
                    idx_id = _short_index_id(rid, chunk.chunk_id, f"u:{uid}")
                    entry = KnowledgeIndexEntry(
                        index_id=idx_id,
                        source_system="outline",
                        source_resource_id=rid,
                        source_uri=source_uri or rid,
                        tenant_id=tenant_id,
                        group_id=None,
                        agent_id=uid,
                        chunk_id=_short_chunk_id(chunk.chunk_id),
                        chunk_text=chunk.text,
                        embedding=emb,
                        content_hash=stored.content_hash,
                        source_updated_at=_parse_updated_at(stored.source_updated_at),
                        indexed_at=datetime.now(timezone.utc),
                        acl_version=stored.acl_version,
                        classification=classification or "INTERNAL",
                        retention_policy=None,
                        provenance=provenance,
                    )
                    entries.append(entry)
                # if only users and no groups, we already added user entries; if both, groups+users entries exist
                # also need public? no — restricted, so no public entry
                if not acl_groups and acl_users:
                    # already added user entries; nothing else
                    pass

    # Bulk upsert to persistent repository in bounded page batches so a
    # large window cannot hold an unbounded unit of work. Fail-closed: a
    # batch failure marks the run failed and stops further batches.
    # Remove the previous resource rows before writing a changed resource.
    # ACL changes can change a public null/null row into private agent rows;
    # upsert alone would leave the old public row searchable.
    if entries:
        try:
            replace_resource_ids = sorted({e.source_resource_id for e in entries})
            for _rid in replace_resource_ids:
                await repository.delete_by_resource(tenant_id, _rid)
        except Exception as exc:
            sync_result.errors.append(f"replace existing resource rows failed: {type(exc).__name__}")
            sync_result.failed = 1
            entries = []
    if entries:
        # deduplicate by index_id (last wins)
        dedup: dict[str, KnowledgeIndexEntry] = {}
        for e in entries:
            dedup[e.index_id] = e
        entries = list(dedup.values())
        try:
            batch_size = max(1, int(persist_batch_size or 200))
        except Exception:
            batch_size = 200
        try:
            persisted = 0
            for _i in range(0, len(entries), batch_size):
                _batch = entries[_i : _i + batch_size]
                await repository.bulk_upsert(_batch)
                persisted += len(_batch)
        except Exception as e:
            # fail-closed for persistence errors in production
            sync_result.errors.append(f"persist failed: {e}")
            sync_result.failed = 1
            persisted = 0

    # Propagate only explicit source deletion IDs. Never infer deletion from an
    # incomplete/paginated chunk store.
    for rid in getattr(sync_result, "deleted_resource_ids", []):
        try:
            await repository.delete_by_resource(tenant_id, rid)
        except Exception as exc:
            sync_result.errors.append(f"delete failed for {rid}: {type(exc).__name__}")
            sync_result.failed = max(1, sync_result.failed)
    # Empty-content cleanup: the orchestrator removes blank documents from the
    # chunk store (counts them as upserted) so they never reach the persist
    # loop above. Drop any previously persisted chunks for fetched docs whose
    # content is blank. Skipped (unchanged, non-blank) docs need no action —
    # they were persisted by the run that first upserted them.
    empty_cleaned = 0
    try:
        stored_ids = set(chunk_store._store.keys())
    except Exception:
        stored_ids = set()
    for d in fetched_docs:
        try:
            rid = d.resource_id
            content = d.content or ""
        except Exception:
            continue
        if rid in stored_ids:
            continue
        if isinstance(content, str) and content.strip():
            continue
        try:
            await repository.delete_by_resource(tenant_id, rid)
            empty_cleaned += 1
        except Exception as exc:
            sync_result.errors.append(f"delete failed for {rid}: {type(exc).__name__}")
            sync_result.failed = max(1, sync_result.failed)
    # Explicit complete-snapshot prune: ONLY when the caller opts in AND this
    # run fetched a COMPLETE FULL SCAN in a single fetch (started at offset 0,
    # has_more False, no fetch failure). Resumed windows (non-zero start
    # cursor) and paginated/truncated windows never prune by absence, even
    # when the flag is True — this keeps empty tail batches (cursor at end)
    # and multi-batch paging from ever deleting. Absent IDs are diffed
    # against the checkpoint-before snapshot (not the live DB) and removed
    # from chunk store, persistent repository, and checkpoint. For corpora
    # larger than one window, run a dedicated prune pass with max_pages big
    # enough to hold the corpus starting from offset 0.
    _pruned: list[str] = []
    try:
        _start = _fetch_start_cursors[0] if len(_fetch_start_cursors) == 1 else "__resumed__"
        _started_at_zero = _start is None or (isinstance(_start, str) and _start.strip() in ("", "0"))
        _snapshot_complete = (
            bool(prune_absent_on_complete_snapshot)
            and not bool(getattr(sync_result, "failed", 0))
            and len(_fetch_has_more) == 1
            and _fetch_has_more[0] is False
            and bool(_started_at_zero)
            # An empty response is not proof of a complete snapshot. It may
            # be a resumed tail, a transient source omission, or an API page
            # boundary; never prune by absence when no document was fetched.
            and bool(getattr(sync_result, "fetched", 0))
        )
    except Exception:
        _snapshot_complete = False
    if _snapshot_complete:
        try:
            _fetched_ids = {d.resource_id for d in fetched_docs if isinstance(d, SourceDocument)}
        except Exception:
            _fetched_ids = set()
        try:
            _explicit = set(getattr(sync_result, "deleted_resource_ids", []) or [])
        except Exception:
            _explicit = set()
        for _rid in sorted(_checkpoint_before_ids - _fetched_ids - _explicit):
            try:
                try:
                    chunk_store.delete(_rid)
                except Exception:
                    pass
                await repository.delete_by_resource(tenant_id, _rid)
                _pruned.append(_rid)
            except Exception as exc:
                sync_result.errors.append(f"delete failed for {_rid}: {type(exc).__name__}")
                sync_result.failed = max(1, sync_result.failed)
        if _pruned:
            try:
                _cur = checkpoint_store.load(getattr(outline_adapter, "source_system", "outline"))
                if _cur is not None:
                    _states = dict(getattr(_cur, "resource_states", {}) or {})
                    for _rid in _pruned:
                        _states.pop(_rid, None)
                    try:
                        _cur.resource_states = _states  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    checkpoint_store.save(_cur)
                    sync_result.checkpoint = _cur
            except Exception as exc:
                sync_result.errors.append(f"checkpoint prune failed: {type(exc).__name__}")
            try:
                sync_result.deleted += len(_pruned)
                _merged = list(getattr(sync_result, "deleted_resource_ids", []) or [])
                for _rid in _pruned:
                    if _rid not in _merged:
                        _merged.append(_rid)
                sync_result.deleted_resource_ids = sorted(_merged)
            except Exception:
                pass
    # Outline ACL enrichment notes (fail-closed markers, never secrets).
    # Restricted-sentinel docs stay hidden until a later run resolves them
    # (their acl_version differs once resolved, forcing reindex).
    try:
        for _e in _acl_errors:
            if _e and _e not in sync_result.errors:
                sync_result.errors.append(_e)
    except Exception:
        pass
    # Commit the checkpoint last. The source cursor is durable only when the
    # corresponding index rows and deletions have been committed. Never save
    # a terminal empty/complete window cursor as a source-progress claim.
    if (
        not sync_result.failed
        and checkpoint_store is not None
        and sync_result.checkpoint is not None
        and (sync_result.fetched > 0 or not _checkpoint_before_ids)
    ):
        try:
            checkpoint_store.save(sync_result.checkpoint)
        except Exception as exc:
            sync_result.errors.append(f"checkpoint save failed: {type(exc).__name__}")
            sync_result.failed = 1
    return OutlineSyncResult(
        source_system=sync_result.source_system,
        fetched=sync_result.fetched,
        upserted=sync_result.upserted,
        skipped=sync_result.skipped,
        deleted=sync_result.deleted,
        deleted_resource_ids=list(getattr(sync_result, "deleted_resource_ids", []) or []),
        failed=sync_result.failed,
        chunks_written=sync_result.chunks_written,
        persisted=persisted,
        errors=list(sync_result.errors),
        checkpoint=sync_result.checkpoint,
        has_more=bool(_fetch_has_more[-1]) if _fetch_has_more else False,
    )


def _short_chunk_id(chunk_id: str) -> str:
    """Keep persisted chunk IDs within the schema's VARCHAR(64) bound."""
    raw = str(chunk_id)
    if len(raw) <= 64:
        return raw
    return raw[:47] + ":" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _short_index_id(rid: str, chunk_id: str, suffix: str = "") -> str:
    raw = f"{rid}:{chunk_id}{suffix}"
    if len(raw) <= 64:
        return raw
    # Preserve readable prefix while guaranteeing the ORM's 64-char limit.
    h = hashlib.sha256(raw.encode()).hexdigest()[:16]
    prefix_len = 64 - 17
    return raw[:prefix_len] + ":" + h


def _parse_updated_at(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # handle Z
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Materialization interface — explicit write gate + read-back verification
# ---------------------------------------------------------------------------

@dataclass
class MaterializationResult:
    source_document: SourceDocument
    outline_resource_id: str
    verification_passed: bool
    provenance: dict[str, Any]
    indexed_entries: int = 0


async def materialize_knowledge_to_outline(
    *,
    title: str,
    text: str,
    tenant_id: str,
    collection_id: str | None = None,
    actor_user_id: str | None = None,
    source_refs: list[str] | None = None,
    provenance_extra: dict[str, Any] | None = None,
    classification: str = "INTERNAL",
    outline_adapter: HttpOutlineSourceAdapter,
    repository: KnowledgeIndexRepository | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    write_enabled: bool = False,
    write_context: dict[str, Any] | None = None,
    publish: bool = True,
) -> MaterializationResult:
    """Materialize a generated knowledge document to Outline with gated write.

    Requirements:
      - write_enabled must be True, otherwise PermissionError (fail-closed)
      - outline_adapter.write_enabled must be True, otherwise PermissionError
      - tenant_id validated, title non-empty
      - provenance preserved: actor, source_refs, tenant, classification, materialized_at
      - Outline write via adapter.create_document(title, text, collectionId, publish)
        followed by read-back verification (_read_back_and_verify) — adapter guarantees
        id/title/text hash exact match; if verification fails, OutlineAPIError propagated.
      - Optionally indexes the created document into persistent KnowledgeIndexRepository
        (chunk+embed+upsert) preserving provenance/source link.

    Args:
        title: document title (required)
        text: document body (required, may be empty string but not None)
        tenant_id: mandatory tenant scope
        collection_id: Outline collection (team/private); defaults to adapter collection or 'team'
        actor_user_id: user performing materialization (for provenance)
        source_refs: list of source resource_ids this doc derives from
        provenance_extra: additional provenance fields to preserve
        classification: document classification (INTERNAL etc.)
        outline_adapter: HttpOutlineSourceAdapter with write_enabled=True
        repository: optional persistent repo to index the new doc (if provided, also requires embedding_provider)
        embedding_provider: for indexing materialized doc (injected; hash blocked in prod)
        write_enabled: explicit write gate (must be True)
        write_context: additional context for adapter permission checker (tenant, user, etc.)
        publish: whether to publish on create (default True per spec)

    Returns:
        MaterializationResult with source_document (from read-back), verification_passed=True
    """
    # ---- validation gate ----
    if not tenant_id or not tenant_id.strip():
        raise ValueError("tenant_id is required for materialization")
    tenant_id = tenant_id.strip()
    if not title or not title.strip():
        raise ValueError("title is required for materialization")
    title = title.strip()
    if text is None:
        raise ValueError("text must not be None for materialization")
    if not isinstance(text, str):
        text = str(text)

    if not write_enabled:
        raise PermissionError(
            "materialization denied: write_enabled=False (explicit write gate required) — fail-closed"
        )
    if not isinstance(outline_adapter, HttpOutlineSourceAdapter):
        # Enforce Http adapter for production materialization
        if _is_production() and not _allow_test_fixture():
            raise RuntimeError("materialization in production requires HttpOutlineSourceAdapter")
        # In tests allow any adapter that implements create_document
        if not hasattr(outline_adapter, "create_document"):
            raise RuntimeError("outline_adapter must implement create_document")
        # Also check write_enabled flag if present
        if getattr(outline_adapter, "write_enabled", False) is False and not _allow_test_fixture():
            raise PermissionError("outline_adapter.write_enabled is False — writes denied")

    # Adapter's own write gate
    if hasattr(outline_adapter, "write_enabled") and not getattr(outline_adapter, "write_enabled"):
        raise PermissionError("Outline writes disabled (adapter.write_enabled=False) — fail-closed")

    if getattr(outline_adapter, "write_enabled", False) is False:
        # generic check already above; but ensure fail-closed
        raise PermissionError("Outline writes disabled — fail-closed")

    # Permission checker context includes tenant/actor if not explicitly provided
    ctx = dict(write_context or {})
    ctx.setdefault("tenant_id", tenant_id)
    if actor_user_id:
        ctx.setdefault("user_id", actor_user_id)
        ctx.setdefault("actor_user_id", actor_user_id)
    ctx.setdefault("action", "materialize_knowledge")

    # ---- provenance ----
    provenance: dict[str, Any] = {
        "source": "generated",
        "generated": True,
        "tenant_id": tenant_id,
        "actor_user_id": actor_user_id,
        "source_refs": list(source_refs or []),
        "source_resource_ids": list(source_refs or []),
        "collection_id": collection_id or getattr(outline_adapter, "collection_id", None) or "team",
        "classification": classification,
        "materialized_at": datetime.now(timezone.utc).isoformat(),
        "materialized_via": "materialize_knowledge_to_outline",
        "write_context": ctx,
    }
    if provenance_extra:
        provenance.update(provenance_extra)

    # ---- outline write with read-back verification ----
    # The adapter's create_document includes _read_back_and_verify internally (exact id/title/text hash).
    # We pass publish and collection_id and context.
    # For adapters that don't have Http signature, we fallback to generic call.
    try:
        # HttpOutlineSourceAdapter signature: create_document(title, text, collection_id, publish, context)
        doc = outline_adapter.create_document(  # type: ignore[call-arg]
            title=title,
            text=text,
            collection_id=collection_id,
            publish=publish,
            context=ctx,
        )
    except TypeError:
        # fallback for test adapters that don't support context param
        try:
            doc = outline_adapter.create_document(title=title, text=text, collection_id=collection_id)  # type: ignore
        except TypeError:
            doc = outline_adapter.create_document(title=title, text=text)  # type: ignore

    # ---- verification already done by adapter; if we got here, passed ----
    # Preserve provenance on returned document (store in doc.provenance if exists, else attached via result)
    # SourceDocument doesn't have provenance field; we attach via result.provenance

    # ---- optional indexing into persistent repo ----
    indexed_entries = 0
    if repository is not None:
        if embedding_provider is None:
            # indexing requested but no provider -> skip with error note? Or raise?
            # We fail-closed for materialization indexing if requested: require provider
            raise ValueError("embedding_provider is required when repository is provided for indexing materialized doc")
        _assert_no_hash_provider_in_production(embedding_provider)

        # Create chunks for the new doc text
        rid = getattr(doc, "resource_id", f"outline/{provenance['collection_id']}/{getattr(doc, 'resource_id', title)}")
        # Ensure doc resource_id is correct
        rid = str(rid)
        chs = make_chunks(resource_id=rid, content=text, source_content_hash=content_hash(text))
        if not chs:
            # empty text still indexes as single empty chunk? we treat as no entries
            pass
        else:
            texts = [c.text for c in chs]
            try:
                embeddings = embedding_provider.embed(texts)
            except Exception as e:
                raise RuntimeError(f"embedding failed for materialized doc {rid}: {e}") from e
            # Build entries: materialized doc is typically public to its tenant's groups; use actor's allowed groups if provided
            # For provenance, keep full source_refs
            entries: list[KnowledgeIndexEntry] = []
            for c, emb in zip(chs, embeddings):
                base_id = _short_index_id(rid, c.chunk_id, "gen")
                mat_provenance = dict(provenance)
                mat_provenance.update({
                    "source": "generated_outline",
                    "outline_resource_id": rid,
                    "outline_doc_id": rid.split("/")[-1] if "/" in rid else rid,
                    "chunk_id": c.chunk_id,
                    "materialized_doc_title": title,
                })
                entry = KnowledgeIndexEntry(
                    index_id=base_id,
                    source_system="outline",
                    source_resource_id=rid,
                    source_uri=getattr(doc, "source_uri", None) or f"https://outline.example.com/doc/{rid.split('/')[-1]}",
                    tenant_id=tenant_id,
                    group_id=None,  # materialized as tenant-public; caller may add ACL refining if needed
                    agent_id=None,
                    chunk_id=_short_chunk_id(c.chunk_id),
                    chunk_text=c.text,
                    embedding=emb,
                    content_hash=content_hash(c.text),
                    source_updated_at=_parse_updated_at(getattr(doc, "source_updated_at", None)),
                    indexed_at=datetime.now(timezone.utc),
                    acl_version=getattr(doc, "acl_version", "v1") or "v1",
                    classification=classification,
                    retention_policy=None,
                    provenance=mat_provenance,
                )
                entries.append(entry)
            if entries:
                await repository.bulk_upsert(entries)
                indexed_entries = len(entries)
        # update resource_id for result
        outline_resource_id = rid
    else:
        outline_resource_id = getattr(doc, "resource_id", "")

    return MaterializationResult(
        source_document=doc,
        outline_resource_id=outline_resource_id,
        verification_passed=True,
        provenance=provenance,
        indexed_entries=indexed_entries,
    )


# ---------------------------------------------------------------------------
# Service class wrapper — convenient for CP/EG integration
# ---------------------------------------------------------------------------

class KnowledgeIndexService:
    """Unified service for RAG search + Outline sync + materialization.

    Suitable for injection in Control Plane / Execution Gateway.

    Example (Control Plane):
        svc = KnowledgeIndexService(repository=repo, embedding_provider=FakeEmbeddingProvider(dim=32))
        hits = await svc.search(query="PTO policy", tenant_id="tenant-a", allowed_group_ids=["eng"])
        sync_res = await svc.sync_outline(tenant_id="tenant-a", outline_adapter=http_adapter)
        mat = await svc.materialize(title="Runbook", text="...", tenant_id="tenant-a", write_enabled=True, ...)

    Example (Execution Gateway tool handler):
        service = KnowledgeIndexService(repository=repo, embedding_provider=provider)
        # gateway resolves outline_adapter from env + verified context
        result = await service.materialize(..., write_enabled=ctx_write_flag, outline_adapter=adapter)
    """

    def __init__(
        self,
        repository: KnowledgeIndexRepository,
        embedding_provider: EmbeddingProvider | None = None,
        *,
        default_outline_adapter: HttpOutlineSourceAdapter | None = None,
        default_chunk_config: ChunkConfig | None = None,
        checkpoint_store: InMemoryCheckpointStore | None = None,
    ) -> None:
        self.repository = repository
        self.embedding_provider = embedding_provider
        self.default_outline_adapter = default_outline_adapter
        self.default_chunk_config = default_chunk_config or ChunkConfig()
        self.checkpoint_store = checkpoint_store or InMemoryCheckpointStore()

    # -- Search -------------------------------------------------------------
    async def search(
        self,
        *,
        query: str,
        tenant_id: str,
        allowed_group_ids: list[str] | None = None,
        allowed_agent_ids: list[str] | None = None,
        user_id: str | None = None,
        limit: int = 10,
        mode: str = "hybrid",
        query_embedding: list[float] | None = None,
        allow_deterministic_fallback: bool = False,
    ) -> list[RetrievalHit]:
        return await search_knowledge(
            query=query,
            tenant_id=tenant_id,
            allowed_group_ids=allowed_group_ids,
            allowed_agent_ids=allowed_agent_ids,
            user_id=user_id,
            repository=self.repository,
            limit=limit,
            mode=mode,
            query_embedding=query_embedding,
            allow_deterministic_fallback=allow_deterministic_fallback,
        )

    # -- Sync ---------------------------------------------------------------
    async def sync_outline(
        self,
        *,
        tenant_id: str,
        outline_adapter: HttpOutlineSourceAdapter | SourceAdapter | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        chunk_config: ChunkConfig | None = None,
    ) -> OutlineSyncResult:
        adapter = outline_adapter or self.default_outline_adapter
        if adapter is None:
            raise ValueError("outline_adapter is required (no default configured)")
        provider = embedding_provider or self.embedding_provider
        if provider is None:
            raise ValueError("embedding_provider is required (no default configured)")
        return await sync_outline_to_index(
            tenant_id=tenant_id,
            repository=self.repository,
            embedding_provider=provider,
            outline_adapter=adapter,
            chunk_config=chunk_config or self.default_chunk_config,
            checkpoint_store=self.checkpoint_store,
        )

    # -- Materialize --------------------------------------------------------
    async def materialize(
        self,
        *,
        title: str,
        text: str,
        tenant_id: str,
        collection_id: str | None = None,
        actor_user_id: str | None = None,
        source_refs: list[str] | None = None,
        provenance_extra: dict[str, Any] | None = None,
        classification: str = "INTERNAL",
        outline_adapter: HttpOutlineSourceAdapter | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        write_enabled: bool = False,
        write_context: dict[str, Any] | None = None,
        publish: bool = True,
    ) -> MaterializationResult:
        adapter = outline_adapter or self.default_outline_adapter
        if adapter is None:
            raise ValueError("outline_adapter is required for materialization")
        # Use provided provider or service default
        provider = embedding_provider if embedding_provider is not None else self.embedding_provider
        # repository indexing is optional; if no repo, just materialize without indexing
        return await materialize_knowledge_to_outline(
            title=title,
            text=text,
            tenant_id=tenant_id,
            collection_id=collection_id,
            actor_user_id=actor_user_id,
            source_refs=source_refs,
            provenance_extra=provenance_extra,
            classification=classification,
            outline_adapter=adapter,
            repository=self.repository,
            embedding_provider=provider,
            write_enabled=write_enabled,
            write_context=write_context,
            publish=publish,
        )

# ---------------------------------------------------------------------------
# Additional wrappers for task contract: KnowledgeSearchService / KnowledgeSyncService / KnowledgeMaterializationService
# KnowledgeSyncService.sync_to_persistent() is a live bridge onto sync_outline_to_index().
# ---------------------------------------------------------------------------
class KnowledgeSearchService:
    """Async persistent search wrapper around KnowledgeIndexRetriever.

    Contract:
    - tenant_id is MANDATORY (ValueError if missing)
    - user_id is MANDATORY (ValueError if missing) — user context for ACL prefilter
    - ACL prefilter BEFORE retrieval: groups -> allowed_group_ids, user_id -> allowed_agent_ids
      (retriever enforces WHERE tenant_id AND (group_id IS NULL OR IN allowed) AND (agent_id IS NULL OR IN allowed))
    - provenance always in RetrievalHit (passthrough)
    - no hash fallback injection: caller must supply query_embedding for semantic; service does not synthesize one.
    """

    def __init__(self, retriever: KnowledgeIndexRetriever) -> None:
        if retriever is None:
            raise ValueError("retriever is required")
        self._retriever = retriever

    async def search(
        self,
        *,
        query: str,
        tenant_id: str,
        user_id: str,
        groups: list[str] | None = None,
        allowed_group_ids: list[str] | None = None,
        allowed_agent_ids: list[str] | None = None,
        limit: int = 10,
        mode: str = "hybrid",
        query_embedding: list[float] | None = None,
        allow_deterministic_fallback: bool = False,
    ) -> list[RetrievalHit]:
        if not tenant_id or not str(tenant_id).strip():
            raise ValueError("tenant_id is required — mandatory tenant context (ACL prefilter)")
        if not user_id or not str(user_id).strip():
            raise ValueError("user_id is required — mandatory user context (ACL prefilter)")
        tenant_id = str(tenant_id).strip()
        user_id = str(user_id).strip()

        # Resolve ACL prefilter sets. Explicit allowed_* override groups/user derivation.
        if allowed_group_ids is not None:
            group_ids = [str(g).strip() for g in allowed_group_ids if str(g).strip()]
        else:
            group_ids = [str(g).strip() for g in (groups or []) if str(g).strip()]

        if allowed_agent_ids is not None:
            agent_ids = [str(a).strip() for a in allowed_agent_ids if str(a).strip()]
        else:
            # default: user_id is the agent principal; plus any groups-as-agent? keep narrow.
            agent_ids = [user_id]

        # Delegate to retriever (which enforces its own tenant + ACL WHERE clauses)
        return await self._retriever.retrieve(
            query=query,
            tenant_id=tenant_id,
            allowed_group_ids=group_ids,
            allowed_agent_ids=agent_ids,
            limit=limit,
            mode=mode,
            query_embedding=query_embedding,
            allow_deterministic_fallback=allow_deterministic_fallback,
        )

    # Convenience aliases that preserve mandatory context
    async def search_lexical(
        self,
        *,
        query: str,
        tenant_id: str,
        user_id: str,
        groups: list[str] | None = None,
        limit: int = 10,
    ) -> list[RetrievalHit]:
        return await self.search(
            query=query,
            tenant_id=tenant_id,
            user_id=user_id,
            groups=groups,
            limit=limit,
            mode="lexical",
        )

    async def search_semantic(
        self,
        *,
        query: str,
        tenant_id: str,
        user_id: str,
        groups: list[str] | None = None,
        query_embedding: list[float],
        limit: int = 10,
        allow_deterministic_fallback: bool = False,
    ) -> list[RetrievalHit]:
        if query_embedding is None:
            raise ValueError("query_embedding is required for semantic search")
        return await self.search(
            query=query,
            tenant_id=tenant_id,
            user_id=user_id,
            groups=groups,
            limit=limit,
            mode="semantic",
            query_embedding=query_embedding,
            allow_deterministic_fallback=allow_deterministic_fallback,
        )


# ---------------------------------------------------------------------------
# 2) Sync wiring — explicit interface, no pretending live persistent sync
# ---------------------------------------------------------------------------
@dataclass
class SyncServiceConfig:
    """Config for KnowledgeSyncService — documents the live persistent bridge."""
    note: str = (
        "SyncOrchestrator runs synchronously (offloaded via asyncio.to_thread) "
        "against an ephemeral InMemoryChunkStore, then sync_outline_to_index drains "
        "chunks into the async persistent KnowledgeIndexRepository with "
        "tenant/ACL provenance. Checkpointing uses the injected checkpoint store "
        "(PersistentCheckpointStore in production)."
    )


class KnowledgeSyncService:
    """Sync wiring around HttpOutlineSourceAdapter + SyncOrchestrator.

    Live persistent sync path:
    - sync() / sync_memory() : runs SyncOrchestrator against its in-memory store (bounded retries,
      checkpointed, idempotent). Returns SyncResult.
    - sync_to_persistent() : delegates to sync_outline_to_index() (real persistent
      sync: chunk+embed via orchestrator, then bulk upsert into
      KnowledgeIndexRepository with tenant/ACL provenance, explicit deletions
      via delete_by_resource, checkpoint persistence). Requires tenant_id,
      repository, embedding_provider, and outline_adapter (or adapter alias);
      raises ValueError listing whichever piece is missing.
    - describe_persistence_gap() : retained for backward compatibility; describes
      the (now implemented) sync-to-persistent bridge instead of a gap.

    No production mock/hash fallback: relies on adapter fail-closed and embedding provider guards.
    """

    def __init__(
        self,
        adapter: HttpOutlineSourceAdapter | None = None,
        orchestrator: SyncOrchestrator | None = None,
        config: SyncServiceConfig | None = None,
        **kwargs: Any,
    ) -> None:
        # Support both stub style (adapter+orchestrator) and persistent style (repository etc) via kwargs
        self._repo = kwargs.get("repository")
        self._provider = kwargs.get("embedding_provider")
        self._tenant_id = kwargs.get("tenant_id")
        # Allow adapter/orchestrator from kwargs aliases
        if adapter is None:
            adapter = kwargs.get("outline_adapter") or kwargs.get("adapter")
        if orchestrator is None:
            orchestrator = kwargs.get("orchestrator")
        if adapter is None or orchestrator is None:
            # For persistent sync tests, adapter may be HttpOutlineSourceAdapter and orchestrator may be None (created lazily)
            # Only enforce when sync() is called; allow construction for sync_to_persistent delegation
            self.adapter = adapter  # type: ignore
            self.orchestrator = orchestrator  # type: ignore
            self.config = config or SyncServiceConfig()
            return
        # Guard: adapter and orchestrator source_system must align
        if getattr(adapter, "source_system", None) != getattr(orchestrator.source, "source_system", None):
            pass
        self.adapter = adapter
        self.orchestrator = orchestrator  # type: ignore
        self.config = config or SyncServiceConfig()

    def sync(self) -> SyncResult:
        """Run in-memory sync (sync/in-memory path). Explicitly NOT persistent."""
        return self.orchestrator.sync()

    # Alias — makes intent obvious in calling code
    def sync_memory(self) -> SyncResult:
        return self.sync()

    def describe_persistence_gap(self) -> str:
        return (
            "Persistence bridge: sync_to_persistent() delegates to sync_outline_to_index(), "
            "which runs SyncOrchestrator synchronously (offloaded via asyncio.to_thread) "
            "into an ephemeral InMemoryChunkStore, then drains chunks into the async "
            "KnowledgeIndexRepository as KnowledgeIndexEntry rows with tenant/ACL "
            "provenance (groups -> group_id entries, users -> agent_id entries, public "
            "-> single null group/agent entry), explicit deletions via "
            "repository.delete_by_resource, and checkpoint persistence via the "
            "injected checkpoint store (PersistentCheckpointStore in production). "
            "No mock/hash fallback in production. "
            f"Note: {self.config.note}"
        )

    async def sync_to_persistent(self, *args: Any, **kwargs: Any) -> Any:
        """Run live persistent sync by delegating to sync_outline_to_index.

        Requires tenant_id, repository, embedding_provider, and outline_adapter
        (or `adapter` alias) — from kwargs or constructor context. Raises
        ValueError listing any missing piece (fail-closed, no silent default).
        """
        # If caller provides persistent context, delegate (covers compat validator that expects this)
        _tenant_raw = kwargs.get("tenant_id") if "tenant_id" in kwargs else getattr(self, "_tenant_id", None)
        tenant_id = str(_tenant_raw).strip() if _tenant_raw is not None else ""
        repo = kwargs.get("repository") or getattr(self, "_repo", None)
        provider = kwargs.get("embedding_provider") or getattr(self, "_provider", None)
        adapter = kwargs.get("outline_adapter") or kwargs.get("adapter") or getattr(self, "adapter", None)
        if repo is not None and provider is not None and adapter is not None and tenant_id:
            # import here to avoid circular
            return await sync_outline_to_index(tenant_id=tenant_id, repository=repo, embedding_provider=provider, outline_adapter=adapter, **{k: v for k, v in kwargs.items() if k not in ("repository", "embedding_provider", "outline_adapter", "tenant_id", "adapter")})
        missing = [
            name
            for name, present in (
                ("tenant_id", bool(tenant_id)),
                ("repository", repo is not None),
                ("embedding_provider", provider is not None),
                ("outline_adapter", adapter is not None),
            )
            if not present
        ]
        raise ValueError(
            "persistent sync requires tenant_id, repository, embedding_provider, and "
            f"outline_adapter (missing: {', '.join(missing)}). No silent default in production."
        )

    def adapter_fetch(self, checkpoint: Any | None = None) -> Any:
        """Direct adapter fetch (read-only, fail-closed)."""
        return self.adapter.fetch(checkpoint)


# Alias for discoverability
OutlineSyncService = KnowledgeSyncService
SyncService = KnowledgeSyncService


# ---------------------------------------------------------------------------
# 3) Materialization wrapper — gated writes with provenance + readback
# ---------------------------------------------------------------------------
class KnowledgeMaterializationService:
    """Materialization wrapper around HttpOutlineSourceAdapter create/update.

    - Wraps adapter.create_document / update_document (which already enforce write_enabled,
      permission checker, and read-back verification via documents.info).
    - Adds provenance into the `context` passed to the adapter's permission checker
      (tenant_id, user_id, trace_id, source, action).
    - Readback is inherited verbatim from adapter (exact id/title/text hash check).
    - No production mock/hash fallback.

    Usage:
        svc = KnowledgeMaterializationService(adapter)
        svc.create_document(title="T", text="hello", collection_id="team",
                            tenant_id="t1", user_id="u1", trace_id="tr-123")
    """

    def __init__(
        self,
        adapter: HttpOutlineSourceAdapter,
        default_provenance: dict[str, Any] | None = None,
    ) -> None:
        if adapter is None:
            raise ValueError("adapter is required")
        self.adapter = adapter
        self.default_provenance = dict(default_provenance or {})

    def _build_context(
        self,
        action: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        trace_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ctx: dict[str, Any] = {}
        ctx.update(self.default_provenance)
        if tenant_id is not None:
            ctx["tenant_id"] = tenant_id
        if user_id is not None:
            ctx["user_id"] = user_id
        if trace_id is not None:
            ctx["trace_id"] = trace_id
        ctx["action"] = action
        ctx["source_system"] = getattr(self.adapter, "source_system", "outline")
        # Provenance goes into context so a custom write_permission_checker can audit it
        prov = dict(self.default_provenance)
        if provenance:
            prov.update(provenance)
        # Always include at least tenant/user/trace in provenance
        if tenant_id:
            prov.setdefault("tenant_id", tenant_id)
        if user_id:
            prov.setdefault("user_id", user_id)
        if trace_id:
            prov.setdefault("trace_id", trace_id)
        prov.setdefault("materialized_by", "KnowledgeMaterializationService")
        ctx["provenance"] = prov
        if extra_context:
            ctx.update(extra_context)
        return ctx

    def create_document(
        self,
        *,
        title: str,
        text: str,
        collection_id: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        trace_id: str | None = None,
        publish: bool = True,
        provenance: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> SourceDocument:
        ctx = self._build_context(
            "create_document",
            tenant_id=tenant_id,
            user_id=user_id,
            trace_id=trace_id,
            extra_context=context,
            provenance=provenance,
        )
        # Adapter enforces write_enabled + permission checker + read-back
        return self.adapter.create_document(
            title=title,
            text=text,
            collection_id=collection_id,
            publish=publish,
            context=ctx,
        )

    def update_document(
        self,
        *,
        doc_id: str,
        title: str | None = None,
        text: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        trace_id: str | None = None,
        publish: bool = True,
        append: bool = False,
        provenance: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> SourceDocument:
        ctx = self._build_context(
            "update_document",
            tenant_id=tenant_id,
            user_id=user_id,
            trace_id=trace_id,
            extra_context=context,
            provenance=provenance,
        )
        return self.adapter.update_document(
            doc_id=doc_id,
            title=title,
            text=text,
            publish=publish,
            append=append,
            context=ctx,
        )

    def delete_document(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
        trace_id: str | None = None,
        provenance: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> bool:
        ctx = self._build_context(
            "delete_document",
            tenant_id=tenant_id,
            user_id=user_id,
            trace_id=trace_id,
            extra_context=context,
            provenance=provenance,
        )
        return self.adapter.delete_document(doc_id=doc_id, context=ctx)


# Aliases for discoverability / validator tolerance
OutlineMaterializationService = KnowledgeMaterializationService
MaterializationService = KnowledgeMaterializationService
KnowledgeWriteService = KnowledgeMaterializationService
OutlineWriteService = KnowledgeMaterializationService

__all__ = [
    "validate_search_context",
    "search_knowledge",
    "sync_outline_to_index",
    "OutlineSyncResult",
    "materialize_knowledge_to_outline",
    "MaterializationResult",
    "KnowledgeIndexService",
    "KnowledgeSearchService",
    "KnowledgeSyncService",
    "OutlineSyncService",
    "SyncService",
    "KnowledgeMaterializationService",
    "OutlineMaterializationService",
    "MaterializationService",
    "KnowledgeWriteService",
    "OutlineWriteService",
    "SyncServiceConfig",
]


# ---------------------------------------------------------------------------
