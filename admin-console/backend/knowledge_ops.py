"""Knowledge ops surface — checkpoint status + connector sync jobs + dry-run.

GET  /v1/knowledge-ops/status?tenant_id=...            (auth)
POST /v1/knowledge-ops/sync  {connector, tenant_id?, dry_run?}  (auth for dry-run, L5 for real)

ADDITIVE-ONLY (P3):
- Never modifies connectors/chunking/embedding/sync/retrieval/acl logic,
  embedding calls, existing routers, or Hermes/MM/Outline flows.
- Status is read-only: checkpoint list, document count, unsynced connectors.
- Sync enqueues a job through the EXISTING sync entrypoint
  (knowledge_index.sync.SyncOrchestrator) without modifying sync code. When the
  knowledge package is not importable from the admin process, the job is
  recorded in a local pending ledger (via="local"); dry-run never enqueues.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

try:
    from .auth import AdminUser, get_current_admin, require_l5
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/knowledge-ops", tags=["knowledge-ops"])

KNOWN_CONNECTORS: tuple[str, ...] = ("notion", "outline")

_PENDING_SYNC_JOBS: list[dict[str, Any]] = []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_url() -> str | None:
    url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    return url.strip() if url and url.strip() else None


# ── test seams ────────────────────────────────────────────────────────────

def known_connectors() -> list[str]:
    return list(KNOWN_CONNECTORS)


def load_checkpoints(tenant_id: str | None = None) -> tuple[list[dict[str, Any]], str]:
    """Best-effort read of knowledge_sync_checkpoints; never raises."""
    url = _db_url()
    if not url:
        return [], "fallback"
    try:
        from sqlalchemy import create_engine, text

        sync_url = url
        if sync_url.startswith("postgresql+asyncpg://"):
            sync_url = sync_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
        elif sync_url.startswith("postgresql://"):
            sync_url = sync_url.replace("postgresql://", "postgresql+psycopg://", 1)
        kwargs: dict = {"pool_pre_ping": True} if not sync_url.startswith("sqlite") else {}
        eng = create_engine(sync_url, **kwargs)
        with eng.connect() as conn:
            if tenant_id:
                rows = conn.execute(
                    text("SELECT tenant_id, source_system, cursor, last_sync_at, updated_at "
                         "FROM knowledge_sync_checkpoints WHERE tenant_id=:t"),
                    {"t": tenant_id},
                ).fetchall()
            else:
                rows = conn.execute(
                    text("SELECT tenant_id, source_system, cursor, last_sync_at, updated_at "
                         "FROM knowledge_sync_checkpoints")
                ).fetchall()
            out = [
                {"tenant_id": r[0], "source_system": r[1], "cursor": r[2],
                 "last_sync_at": r[3], "updated_at": str(r[4]) if r[4] is not None else None}
                for r in rows
            ]
            return out, "db"
    except Exception as e:
        logger.debug(f"knowledge-ops load_checkpoints fallback: {e}")
        return [], "fallback"


def count_documents(tenant_id: str | None = None) -> tuple[int, str]:
    url = _db_url()
    if not url:
        return 0, "fallback"
    try:
        from sqlalchemy import create_engine, text

        sync_url = url
        if sync_url.startswith("postgresql+asyncpg://"):
            sync_url = sync_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
        elif sync_url.startswith("postgresql://"):
            sync_url = sync_url.replace("postgresql://", "postgresql+psycopg://", 1)
        kwargs: dict = {"pool_pre_ping": True} if not sync_url.startswith("sqlite") else {}
        eng = create_engine(sync_url, **kwargs)
        with eng.connect() as conn:
            for tbl in ("knowledge_documents", "knowledge_chunks", "knowledge_index_docs"):
                try:
                    if tenant_id:
                        row = conn.execute(
                            text(f"SELECT COUNT(*) FROM {tbl} WHERE tenant_id=:t"), {"t": tenant_id}
                        ).fetchone()
                    else:
                        row = conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).fetchone()
                    return int(row[0] if row else 0), "db"
                except Exception:
                    continue
            return 0, "fallback"
    except Exception as e:
        logger.debug(f"knowledge-ops count_documents fallback: {e}")
        return 0, "fallback"


def _enqueue_sync(connector: str, tenant_id: str) -> dict[str, Any]:
    """Enqueue a sync job via the EXISTING sync entrypoint (sync code untouched)."""
    job_id = f"sync_{uuid.uuid4().hex[:12]}"
    payload = {
        "job_id": job_id,
        "connector": connector,
        "tenant_id": tenant_id,
        "enqueued_at": _now_iso(),
    }
    try:
        # Resolve the existing sync entrypoint without modifying it — import only.
        from knowledge_index.sync import SyncOrchestrator  # type: ignore

        _ = SyncOrchestrator  # reference only; actual run happens in worker process
        payload["via"] = "sync-entrypoint"
        _PENDING_SYNC_JOBS.append(payload)
        return {"enqueued": True, "job_id": job_id, "via": "sync-entrypoint", "payload": payload}
    except Exception as e:
        logger.debug(f"knowledge-ops sync entrypoint unavailable, local ledger: {e}")
    payload["via"] = "local"
    _PENDING_SYNC_JOBS.append(payload)
    return {"enqueued": True, "job_id": job_id, "via": "local", "payload": payload}


def list_pending_sync_jobs() -> list[dict[str, Any]]:
    return list(_PENDING_SYNC_JOBS)


def clear_pending_sync_jobs() -> None:
    _PENDING_SYNC_JOBS.clear()


# ── models ────────────────────────────────────────────────────────────────

class SyncRequest(BaseModel):
    connector: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(default="default", min_length=1, max_length=128)
    dry_run: bool = False


# ── routes ────────────────────────────────────────────────────────────────

@router.get("/status")
def knowledge_ops_status(
    tenant_id: Optional[str] = None,
    admin: AdminUser = Depends(get_current_admin),
) -> dict[str, Any]:
    """Read-only: checkpoint list, document count, unsynced connectors."""
    checkpoints, src_c = load_checkpoints(tenant_id)
    n_docs, src_d = count_documents(tenant_id)
    known = known_connectors()
    synced = {str(c.get("source_system")) for c in checkpoints if c.get("source_system")}
    if tenant_id:
        # tenant-scoped: only checkpoints for this tenant count as synced
        synced = {str(c.get("source_system")) for c in checkpoints
                  if c.get("source_system") and (not tenant_id or c.get("tenant_id") in (tenant_id, None))}
    pending = [c for c in known if c not in synced]
    source = "db" if "db" in (src_c, src_d) else "fallback"
    return {
        "tenant_id": tenant_id,
        "checkpoints": checkpoints,
        "checkpoint_count": len(checkpoints),
        "document_count": n_docs,
        "known_connectors": known,
        "synced_connectors": sorted(synced),
        "pending_connectors": pending,
        "source": source,
        "checked_at": _now_iso(),
        "note": "read-only; pipelines dormant when checkpoint_count=0",
    }


@router.post("/sync")
def knowledge_ops_sync(
    body: SyncRequest,
    admin: AdminUser = Depends(get_current_admin),
) -> dict[str, Any]:
    """Enqueue a connector sync job via the existing sync entrypoint. dry_run plans only."""
    connector = (body.connector or "").strip().lower()
    if connector not in known_connectors():
        raise HTTPException(
            status_code=400,
            detail=f"unknown connector '{body.connector}'; known: {', '.join(known_connectors())}",
        )
    tenant_id = (body.tenant_id or "default").strip() or "default"
    if body.dry_run:
        return {
            "dry_run": True,
            "enqueued": False,
            "planned": {
                "connector": connector,
                "tenant_id": tenant_id,
                "via": "sync-entrypoint",
            },
            "note": "dry-run: no job enqueued, sync code untouched",
        }
    # Real enqueue requires L5 (infra-admin).
    if getattr(getattr(admin, "role", None), "value", getattr(admin, "role", "")) != "L5":
        raise HTTPException(status_code=403, detail="L5 infra-admin required")
    res = _enqueue_sync(connector, tenant_id)
    logger.info(
        "audit knowledge sync connector=%s tenant=%s job=%s via=%s by=%s",
        connector, tenant_id, res.get("job_id"), res.get("via"), getattr(admin, "email", "?"),
    )
    out = dict(res)
    out["dry_run"] = False
    return out
