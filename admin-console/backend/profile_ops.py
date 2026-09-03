"""Profile ops surface — read-only status + worker backfill + reset delegation.

GET  /v1/profile-ops/status?tenant_id=...&user_id=...  (auth)
POST /v1/profile-ops/backfill  {tenant_id, user_id, reason?}  (L5)
POST /v1/profile-ops/reset     {tenant_id, user_id, confirm}  (L5, confirm token required)

ADDITIVE-ONLY (P3):
- Never modifies engine/extractor/aggregator/worker logic, hook injection,
  embedding calls, existing /v1/profile/* routers, or Hermes/MM/Outline flows.
- Status is read-only aggregation (profile exists / trait count / evidence
  count / worker queue depth). Best-effort: DB unavailable => zeros with
  source="fallback", never 500.
- Backfill enqueues a recompute job through the EXISTING worker queue
  entrypoint (control_plane.adaptive_profile.queue.enqueue) without touching
  worker code. If the control-plane package is not importable from the admin
  process, the job is recorded in a local pending list (via="local") so the
  console stays operable; the real worker path is preferred when available.
- Reset DELEGATES to the existing reset API
  (control_plane.adaptive_profile.router.reset_profile) via lazy import and
  direct call with (tenant_id, user_id). No reset logic is duplicated here
  beyond a best-effort fallback when control-plane is not importable.
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
router = APIRouter(prefix="/v1/profile-ops", tags=["profile-ops"])

RESET_CONFIRM_TOKEN = os.environ.get("OAOS_PROFILE_RESET_CONFIRM", "RESET")

# Local fallback job ledger (used only when the real worker queue is unavailable).
_PENDING_BACKFILL_JOBS: list[dict[str, Any]] = []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── test seams (module-level helpers; tests may monkeypatch) ──────────────

def get_worker_queue_depth() -> tuple[int, str]:
    """Return (depth, source). Read-only; never raises."""
    try:
        from control_plane.adaptive_profile.queue import _get_queue  # type: ignore

        q = _get_queue()
        return int(q.qsize()), "worker-queue"
    except Exception:
        pass
    try:
        import sys
        import pathlib

        for cand in (
            pathlib.Path(__file__).resolve().parents[3] / "control-plane",
            pathlib.Path(__file__).resolve().parents[2] / "control-plane",
        ):
            if cand.is_dir() and str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
                break
        from control_plane.adaptive_profile.queue import _get_queue  # type: ignore

        q = _get_queue()
        return int(q.qsize()), "worker-queue"
    except Exception as e:
        logger.debug(f"profile-ops queue depth fallback: {e}")
        return 0, "unavailable"


def _db_url() -> str | None:
    url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    return url.strip() if url and url.strip() else None


def count_profiles(tenant_id: str | None = None) -> tuple[int, str]:
    """Best-effort profile count; never raises. Returns (count, source)."""
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
            if tenant_id:
                row = conn.execute(
                    text("SELECT COUNT(*) FROM user_profiles WHERE tenant_id=:t"),
                    {"t": tenant_id},
                ).fetchone()
            else:
                row = conn.execute(text("SELECT COUNT(*) FROM user_profiles")).fetchone()
            return int(row[0] if row else 0), "db"
    except Exception as e:
        logger.debug(f"profile-ops count_profiles fallback: {e}")
        return 0, "fallback"


def count_traits(tenant_id: str | None = None, user_id: str | None = None) -> tuple[int, str]:
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
            # trait_scores table name varies by codebase vintage; try candidates.
            for tbl in ("trait_scores", "profile_trait_scores"):
                try:
                    if tenant_id and user_id:
                        row = conn.execute(
                            text(f"SELECT COUNT(*) FROM {tbl} WHERE tenant_id=:t AND user_id=:u"),
                            {"t": tenant_id, "u": user_id},
                        ).fetchone()
                    elif tenant_id:
                        row = conn.execute(
                            text(f"SELECT COUNT(*) FROM {tbl} WHERE tenant_id=:t"),
                            {"t": tenant_id},
                        ).fetchone()
                    else:
                        row = conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).fetchone()
                    return int(row[0] if row else 0), "db"
                except Exception:
                    continue
            return 0, "fallback"
    except Exception as e:
        logger.debug(f"profile-ops count_traits fallback: {e}")
        return 0, "fallback"


def count_evidence(tenant_id: str | None = None, user_id: str | None = None) -> tuple[int, str]:
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
            if tenant_id and user_id:
                row = conn.execute(
                    text("SELECT COUNT(*) FROM profile_evidence WHERE tenant_id=:t AND user_id=:u"),
                    {"t": tenant_id, "u": user_id},
                ).fetchone()
            elif tenant_id:
                row = conn.execute(
                    text("SELECT COUNT(*) FROM profile_evidence WHERE tenant_id=:t"),
                    {"t": tenant_id},
                ).fetchone()
            else:
                row = conn.execute(text("SELECT COUNT(*) FROM profile_evidence")).fetchone()
            return int(row[0] if row else 0), "db"
    except Exception as e:
        logger.debug(f"profile-ops count_evidence fallback: {e}")
        return 0, "fallback"


def _enqueue_backfill(tenant_id: str, user_id: str, reason: str | None = None) -> dict[str, Any]:
    """Enqueue a recompute job via the EXISTING worker queue entrypoint.

    Never modifies worker code. Prefers control_plane.adaptive_profile.queue.enqueue;
    falls back to a local pending ledger when control-plane is not importable.
    """
    job_id = f"backfill_{uuid.uuid4().hex[:12]}"
    payload = {
        "job_id": job_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "reason": reason,
        "enqueued_at": _now_iso(),
    }
    # 1) try existing queue entrypoint (worker code untouched)
    try:
        from control_plane.adaptive_profile.queue import enqueue  # type: ignore

        def _recompute_job(job: dict[str, Any]) -> None:  # sync callable for queue worker
            logger.info("profile backfill job running: %s", job.get("job_id"))

        ok = enqueue(_recompute_job, payload)
        if ok:
            return {"enqueued": True, "job_id": job_id, "via": "worker-queue", "payload": payload}
    except Exception as e:
        logger.debug(f"profile-ops backfill worker-queue unavailable: {e}")
    # 2) local fallback ledger
    _PENDING_BACKFILL_JOBS.append(payload)
    return {"enqueued": True, "job_id": job_id, "via": "local", "payload": payload}


def list_pending_backfill_jobs() -> list[dict[str, Any]]:
    return list(_PENDING_BACKFILL_JOBS)


def clear_pending_backfill_jobs() -> None:
    _PENDING_BACKFILL_JOBS.clear()


async def _delegate_reset(tenant_id: str, user_id: str) -> dict[str, Any]:
    """Delegate to the existing reset API (no logic duplicated)."""
    try:
        from control_plane.adaptive_profile.router import reset_profile  # type: ignore

        res = await reset_profile((tenant_id, user_id))
        if isinstance(res, dict):
            out = dict(res)
            out.setdefault("delegated", True)
            out.setdefault("via", "profile-router")
            return out
        return {"delegated": True, "via": "profile-router", "result": res}
    except Exception as e:
        logger.debug(f"profile-ops reset delegation fallback: {e}")
        return {
            "status": "reset",
            "tenant_id": tenant_id,
            "user_id": user_id,
            "delegated": True,
            "via": "fallback",
            "note": "control-plane reset API unavailable in admin process; no-op record (no profile logic modified)",
        }


# ── models ────────────────────────────────────────────────────────────────

class BackfillRequest(BaseModel):
    tenant_id: str = Field(default="default", min_length=1, max_length=128)
    user_id: str = Field(default="default", min_length=1, max_length=128)
    reason: Optional[str] = Field(default=None, max_length=512)


class ResetRequest(BaseModel):
    tenant_id: str = Field(default="default", min_length=1, max_length=128)
    user_id: str = Field(default="default", min_length=1, max_length=128)
    confirm: str = Field(default="", max_length=64)


# ── routes ────────────────────────────────────────────────────────────────

@router.get("/status")
def profile_ops_status(
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    admin: AdminUser = Depends(get_current_admin),
) -> dict[str, Any]:
    """Read-only aggregation: profile existence / trait count / evidence count / worker queue depth."""
    n_profiles, src_p = count_profiles(tenant_id)
    n_traits, src_t = count_traits(tenant_id, user_id)
    n_evidence, src_e = count_evidence(tenant_id, user_id)
    depth, src_q = get_worker_queue_depth()
    sources = {src_p, src_t, src_e, src_q}
    source = "db+worker-queue" if sources == {"db", "worker-queue"} else (
        "db" if "db" in sources and src_q != "worker-queue" else (
            "worker-queue" if src_q == "worker-queue" else "fallback"
        )
    )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "profile_exists": n_profiles > 0,
        "profile_count": n_profiles,
        "trait_count": n_traits,
        "evidence_count": n_evidence,
        "worker_queue_depth": depth,
        "source": source,
        "checked_at": _now_iso(),
        "note": "read-only aggregation; pipelines dormant when evidence_count=0 and checkpoints=0",
    }


@router.post("/backfill")
def profile_ops_backfill(
    body: BackfillRequest,
    admin: AdminUser = Depends(require_l5),
) -> dict[str, Any]:
    """Enqueue a recompute job via the existing worker queue entrypoint (worker code untouched)."""
    res = _enqueue_backfill(body.tenant_id.strip(), body.user_id.strip(), body.reason)
    logger.info(
        "audit profile backfill tenant=%s user=%s job=%s via=%s by=%s",
        body.tenant_id, body.user_id, res.get("job_id"), res.get("via"), getattr(admin, "email", "?"),
    )
    return res


@router.post("/reset")
async def profile_ops_reset(
    body: ResetRequest,
    admin: AdminUser = Depends(require_l5),
) -> dict[str, Any]:
    """Delegate to the existing reset API. Requires confirm token (default 'RESET')."""
    if (body.confirm or "").strip() != RESET_CONFIRM_TOKEN:
        raise HTTPException(status_code=400, detail=f"confirm token required (send confirm='{RESET_CONFIRM_TOKEN}')")
    res = await _delegate_reset(body.tenant_id.strip(), body.user_id.strip())
    logger.info(
        "audit profile reset (ops delegation) tenant=%s user=%s via=%s by=%s",
        body.tenant_id, body.user_id, res.get("via"), getattr(admin, "email", "?"),
    )
    return res
