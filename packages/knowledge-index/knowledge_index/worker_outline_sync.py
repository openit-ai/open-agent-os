"""Durable, bounded Outline -> Knowledge Index sync worker (read-only source).

Runs incremental Outline sync in bounded page batches with durable PostgreSQL
persistence per batch:

  Outline API (read-only HttpOutlineSourceAdapter)
    -> chunk -> Ollama embed (/api/embed) -> KnowledgeIndexRepository (async)
    + PersistentCheckpointStore (PostgreSQL, shared across batches)

Canonical env (no new names):
  Tenant:     --tenant | OAOS_TENANT_ID | OAOS_CP_TENANT_ID | TENANT_ID
  Database:   --database-url | OAOS_DATABASE_URL | DATABASE_URL
              (asyncpg URL, e.g. postgresql+asyncpg://...; sync checkpoint
              derives postgresql+psycopg:// automatically)
  Outline:    OUTLINE_API_URL | OAOS_OUTLINE_URL | OAOS_OUTLINE_API_URL
              OUTLINE_API_KEY | OUTLINE_API_TOKEN | OAOS_OUTLINE_TOKEN | OAOS_OUTLINE_API_KEY
  Embeddings: OAOS_EMBED_API_URL (e.g. http://127.0.0.1:11434)
              OAOS_EMBED_MODEL (default bge-m3:latest)
              OAOS_EMBED_DIM (default 1024 for bge-m3, else provider default)
  Collection: --collection-id | OAOS_OUTLINE_COLLECTION_ID

Safety:
- Read-only Outline: write_enabled is never enabled here.
- No implicit create_all: tables must already exist (migration-managed). A
  missing schema surfaces as an explicit deployment error.
- No mock/fake in production: without OAOS_EMBED_API_URL the worker fails
  closed in production. Fake embeddings require BOTH non-production env AND
  --allow-fake-embed (explicit test/dev opt-in, never implicit).
- Deletions only from explicit source deletion IDs, blank-content cleanup,
  or explicit --prune-absent on a COMPLETE snapshot inside
  sync_outline_to_index; paginated/truncated window absence never deletes.
- --dry-run fetches a single bounded page and prints counts only: no embedding
  call, no DB connection, no mutation.

Modes: one-shot by default (bounded batches until drained or max-batches);
--loop repeats the one-shot run every --interval-s seconds until interrupted
(SIGHUP/SIGINT). The worker is never started by this change — run it
explicitly (e.g. `python -m knowledge_index.worker_outline_sync --dry-run`).

Progress: per-batch and cumulative counters print to stdout; final JSON
summary prints at the end. Each batch commits repository rows + checkpoint
before the next begins, so a failed run resumes via the saved cursor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any


def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        if os.environ.get(k, "").strip().lower() in ("production", "prod"):
            return True
    return False


def _resolve_tenant(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    for k in ("OAOS_TENANT_ID", "OAOS_CP_TENANT_ID", "TENANT_ID"):
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return ""


def _resolve_db_url(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    return (os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()


def _asyncpg_url(db_url: str) -> str:
    if db_url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + db_url[len("postgresql://"):]
    if db_url.startswith("postgresql+psycopg://"):
        return "postgresql+asyncpg://" + db_url[len("postgresql+psycopg://"):]
    return db_url


def _sync_psycopg_url(db_url: str) -> str:
    if db_url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + db_url[len("postgresql+asyncpg://"):]
    if db_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + db_url[len("postgresql://"):]
    return db_url


def _resolve_embed_dim(model: str, explicit: int | None) -> int | None:
    if explicit is not None:
        return explicit
    raw = os.environ.get("OAOS_EMBED_DIM", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    if "bge-m3" in (model or "").lower():
        return 1024
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bounded Outline -> Knowledge Index sync worker (read-only source).")
    p.add_argument("--tenant", default=None, help="Tenant ID (or OAOS_TENANT_ID / OAOS_CP_TENANT_ID / TENANT_ID).")
    p.add_argument("--collection-id", default=None, help="Outline collection filter (or OAOS_OUTLINE_COLLECTION_ID).")
    p.add_argument("--database-url", default=None, help="DB URL (or OAOS_DATABASE_URL / DATABASE_URL).")
    p.add_argument("--page-limit", type=int, default=25, help="Docs per Outline API page (1..100, default 25).")
    p.add_argument("--max-pages", type=int, default=8, help="Pages per batch window (default 8, >=1).")
    p.add_argument("--max-batches", type=int, default=50, help="Max batch windows per run (default 50, >=1).")
    p.add_argument("--embed-dim", type=int, default=None, help="Embedding dim override (or OAOS_EMBED_DIM).")
    p.add_argument("--dry-run", action="store_true", help="Fetch one bounded page, print counts, mutate nothing.")
    p.add_argument("--allow-fake-embed", action="store_true", help="Dev/test only: allow FakeEmbeddingProvider when no OAOS_EMBED_API_URL (refused in production).")
    p.add_argument("--loop", action="store_true", help="Repeat the bounded one-shot run every --interval-s until interrupted (default: one-shot).")
    p.add_argument("--interval-s", type=float, default=300.0, help="Delay between --loop runs in seconds (default 300, min 5).")
    p.add_argument("--prune-absent", action="store_true", help="Delete tenant resources absent from a COMPLETE snapshot only (passed to sync_outline_to_index; truncated windows never prune). Default off.")
    p.add_argument("--persist-batch-size", type=int, default=200, help="Entries per repository bulk_upsert (default 200, >=1).")
    return p


def do_dry_run(*, page_limit: int, collection_id: str | None) -> dict[str, Any]:
    from .service import create_outline_adapter

    adapter = create_outline_adapter(
        collection_id=collection_id, page_limit=page_limit, timeout_s=8,
    )
    raw, has_more = adapter._fetch_page(offset=0, limit=page_limit)
    out = {
        "dry_run": True,
        "page_limit": page_limit,
        "fetched": len(raw),
        "has_more": has_more,
        "collection_id": collection_id,
    }
    print(f"[dry-run] fetched={len(raw)} has_more={has_more} page_limit={page_limit} (no embed, no DB, no mutation)")
    print(json.dumps(out, indent=2))
    return out


async def do_sync_batches(
    *,
    tenant_id: str,
    collection_id: str | None,
    db_url: str,
    page_limit: int,
    max_pages: int,
    max_batches: int,
    embed_dim: int | None,
    allow_fake_embed: bool,
    prune_absent: bool = False,
    persist_batch_size: int = 200,
) -> dict[str, Any]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from .checkpoint import PersistentCheckpointStore
    from .chunking import ChunkConfig
    from .embedding import FakeEmbeddingProvider, OllamaEmbeddingProvider
    from .repository import KnowledgeIndexRepository
    from .service import create_outline_adapter, sync_outline_to_index

    is_prod = _is_production()
    async_url = _asyncpg_url(db_url)
    if "sqlite" in async_url and is_prod:
        raise RuntimeError("sqlite database URL refused in production — set OAOS_DATABASE_URL to PostgreSQL.")

    # Real embedding provider only; fake requires explicit non-prod opt-in.
    embed_api_url = os.environ.get("OAOS_EMBED_API_URL", "").strip()
    embed_model = os.environ.get("OAOS_EMBED_MODEL", "").strip() or "bge-m3:latest"
    if embed_api_url:
        provider = OllamaEmbeddingProvider(api_url=embed_api_url, model=embed_model, dim=_resolve_embed_dim(embed_model, embed_dim))
        provider_desc = f"ollama model={embed_model}"
    elif is_prod:
        raise RuntimeError(
            "No embedding provider configured in production "
            "(set OAOS_EMBED_API_URL=http://127.0.0.1:11434 + OAOS_EMBED_MODEL=bge-m3:latest). "
            "Fake/hash embeddings are blocked in production."
        )
    elif allow_fake_embed:
        provider = FakeEmbeddingProvider(dim=embed_dim or 1024)
        provider_desc = "fake (explicit --allow-fake-embed, non-prod only)"
    else:
        raise RuntimeError(
            "OAOS_EMBED_API_URL is not set. Refusing implicit fake embeddings — "
            "set OAOS_EMBED_API_URL for a real Ollama provider or pass --allow-fake-embed in non-production."
        )

    # PostgreSQL repo + checkpoint. No create_all: schema is migration-managed.
    engine = create_async_engine(async_url, echo=False, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM knowledge_index LIMIT 1"))
            await conn.execute(text("SELECT 1 FROM knowledge_sync_checkpoints LIMIT 1"))
    except Exception as exc:
        await engine.dispose()
        raise RuntimeError(
            "knowledge schema not present (knowledge_index / knowledge_sync_checkpoints). "
            f"Run migrations first; the worker never creates tables implicitly. ({type(exc).__name__}: {exc})"
        ) from exc
    maker = async_sessionmaker(engine, expire_on_commit=False)
    repo = KnowledgeIndexRepository(maker)

    from sqlalchemy import create_engine as _create_sync_engine

    checkpoint_store = PersistentCheckpointStore(_create_sync_engine(_sync_psycopg_url(db_url), pool_pre_ping=True), tenant_id)

    # --prune-absent runs start from offset 0 (full scan): the per-call prune
    # inside sync_outline_to_index only fires on a complete single-window
    # snapshot starting at offset 0, so a resumed run must not prune. Prune
    # passes need --max-pages large enough to hold the corpus in one window
    # (e.g. 50 x 25 = 1250 for ~900 docs); undersized windows safely prune
    # nothing. If the reset fails we continue without pruning (safe default).
    if prune_absent:
        try:
            _cp = checkpoint_store.load("outline")
            if _cp is not None and getattr(_cp, "cursor", None):
                _cp.cursor = None  # type: ignore[attr-defined]
                checkpoint_store.save(_cp)
                print("[sync] prune-absent: checkpoint cursor reset to 0 for full-scan prune pass")
        except Exception as exc:
            print(f"[sync] prune-absent: cursor reset failed ({type(exc).__name__}); continuing without prune eligibility")

    print(f"[sync] tenant={tenant_id} provider={provider_desc} page_limit={page_limit} max_pages={max_pages} max_batches={max_batches}")
    total = {"fetched": 0, "upserted": 0, "skipped": 0, "deleted": 0, "failed": 0, "chunks_written": 0, "persisted": 0}
    errors: list[str] = []
    batches_run = 0
    last_cursor: str | None = None
    try:
        for batch in range(1, max_batches + 1):
            adapter = create_outline_adapter(
                collection_id=collection_id,
                page_limit=page_limit,
                max_pages=max_pages,
                timeout_s=10.0,
                max_retries=3,
            )
            assert adapter.write_enabled is False  # read-only invariant
            result = await sync_outline_to_index(
                tenant_id=tenant_id,
                repository=repo,
                embedding_provider=provider,
                outline_adapter=adapter,
                chunk_config=ChunkConfig(),
                checkpoint_store=checkpoint_store,
                prune_absent_on_complete_snapshot=bool(prune_absent),
                persist_batch_size=max(1, int(persist_batch_size or 200)),
            )
            batches_run = batch
            for k in total:
                total[k] += int(getattr(result, k, 0) or 0)
            errors.extend(getattr(result, "errors", []) or [])
            cursor = getattr(getattr(result, "checkpoint", None), "cursor", None)
            print(
                f"[batch {batch}] fetched={result.fetched} upserted={result.upserted} "
                f"skipped={result.skipped} deleted={result.deleted} persisted={result.persisted} "
                f"failed={result.failed} cursor={cursor}"
            )
            # The service returns the source truncation bit explicitly. A
            # complete page can be exactly max_pages*page_limit, so length
            # alone cannot distinguish "end of corpus" from "more pages".
            drained = not bool(getattr(result, "has_more", False))
            stalled = cursor is not None and cursor == last_cursor and result.fetched == 0
            last_cursor = cursor
            if result.failed:
                print(f"[batch {batch}] errors: {'; '.join(getattr(result, 'errors', []) or [])[:500]}")
                break
            if result.fetched == 0 or drained or stalled:
                break
    finally:
        await engine.dispose()

    summary = {
        "tenant_id": tenant_id,
        "collection_id": collection_id,
        "provider": provider_desc,
        "page_limit": page_limit,
        "max_pages": max_pages,
        "batches_run": batches_run,
        **total,
        "errors": errors[:20],
    }
    print("[done] " + json.dumps(summary, ensure_ascii=False))
    return summary


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    page_limit = max(1, min(int(args.page_limit), 100))
    max_pages = max(1, int(args.max_pages))
    max_batches = max(1, int(args.max_batches))
    collection_id = (args.collection_id or os.environ.get("OAOS_OUTLINE_COLLECTION_ID") or "").strip() or None

    if args.dry_run:
        try:
            do_dry_run(page_limit=page_limit, collection_id=collection_id)
        except RuntimeError as exc:
            print(f"[dry-run] BLOCKED: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 — CLI must report, not traceback
            print(f"[dry-run] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        return 0

    tenant_id = _resolve_tenant(args.tenant)
    if not tenant_id:
        print("[sync] BLOCKED: tenant is required (--tenant or OAOS_TENANT_ID / OAOS_CP_TENANT_ID / TENANT_ID).", file=sys.stderr)
        return 2
    db_url = _resolve_db_url(args.database_url)
    if not db_url:
        print("[sync] BLOCKED: database URL is required (--database-url or OAOS_DATABASE_URL / DATABASE_URL).", file=sys.stderr)
        return 2
    try:
        interval_s = max(5.0, float(getattr(args, "interval_s", 300.0) or 300.0))
    except Exception:
        interval_s = 300.0
    try:
        persist_batch_size = max(1, int(getattr(args, "persist_batch_size", 200) or 200))
    except Exception:
        persist_batch_size = 200
    prune_absent = bool(getattr(args, "prune_absent", False))
    loop_mode = bool(getattr(args, "loop", False))

    def _run_once() -> dict[str, Any]:
        return asyncio.run(
            do_sync_batches(
                tenant_id=tenant_id,
                collection_id=collection_id,
                db_url=db_url,
                page_limit=page_limit,
                max_pages=max_pages,
                max_batches=max_batches,
                embed_dim=args.embed_dim,
                allow_fake_embed=bool(args.allow_fake_embed),
                prune_absent=prune_absent,
                persist_batch_size=persist_batch_size,
            )
        )

    try:
        summary = _run_once()
        if loop_mode:
            iteration = 1
            while True:
                print(f"[loop] iteration={iteration} sleeping {interval_s:g}s (Ctrl-C to stop)")
                try:
                    import time as _time

                    _time.sleep(interval_s)
                except KeyboardInterrupt:
                    break
                iteration += 1
                summary = _run_once()
                if int(summary.get("failed", 0) or 0):
                    print("[loop] run reported failures; continuing loop", file=sys.stderr)
    except RuntimeError as exc:
        print(f"[sync] BLOCKED: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("[loop] interrupted; exiting", file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001 — CLI must report, not traceback
        print(f"[sync] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 1 if int(summary.get("failed", 0) or 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
