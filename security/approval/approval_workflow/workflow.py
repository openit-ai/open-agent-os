"""JIT Approval Workflow — Section 12, 23-24.
- approval_id / request_hash / nonce / signature
- 4 decisions: DENIED / APPROVED_ONCE / APPROVED_USER_ALWAYS / APPROVED_GROUP_ALWAYS
- expiry check + signature + nonce + hash 검증
- DB persistence: when DATABASE_URL/OAOS_DATABASE_URL is set, approval_requests
  are persisted to approval_requests table (sync SQLAlchemy, sqlite compat).
  Falls back to in-memory dicts. All DB imports are lazy.
- Nonce replay protection: approval_nonces table (nonce TEXT PK, created_at,
  expires_at) with TTL 300s. DB primary, in-memory fallback. Survives restart
  via DB query. postgresql+psycopg when DATABASE_URL is postgres, sqlite for tests.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import uuid
import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel


logger = logging.getLogger(__name__)

try:
    from sqlalchemy.exc import IntegrityError, SQLAlchemyError
except (ImportError, ModuleNotFoundError):  # sqlalchemy is lazy/optional; best-effort fallback
    IntegrityError = RuntimeError  # type: ignore
    SQLAlchemyError = Exception  # type: ignore

# ── constants ──────────────────────────────────────────────────
NONCE_TTL_SECONDS = 300  # token expiry for replay protection


class ApprovalDecision(str, Enum):
    PENDING = "PENDING"
    APPROVED_ONCE = "APPROVED_ONCE"
    APPROVED_USER_ALWAYS = "APPROVED_USER_ALWAYS"
    APPROVED_GROUP_ALWAYS = "APPROVED_GROUP_ALWAYS"
    DENIED = "DENIED"


class ApprovalRequest(BaseModel):
    approval_id: str
    user_id: str
    agent_id: str
    resource: str
    action: str
    risk: str
    request_hash: str
    nonce: str
    expires_at: datetime
    signature: str | None = None
    # 결정 관련
    decision: ApprovalDecision = ApprovalDecision.PENDING
    decided_at: datetime | None = None
    decided_by: str | None = None


# ── DB helpers (lazy, sync) + production fail-closed ───────────────
# Distributed state: DB (approval_requests + approval_nonces) is primary in production.
# In-memory fallback is allowed ONLY in non-prod (explicit test fallback).
# Limitations: in-memory nonce grants are process-local, not distributed — prod MUST use DB.

def _is_prod() -> bool:
    return os.environ.get("OAOS_ENV", "").lower() in ("production", "prod")


def _allow_in_memory_fallback() -> bool:
    if _is_prod():
        return False
    return True


def _require_db_if_prod() -> None:
    if _is_prod() and not _db_enabled():
        raise RuntimeError(
            "ApprovalStore: DATABASE_URL/OAOS_DATABASE_URL required when OAOS_ENV=production "
            "(fail-closed — distributed approval/nonce state must be DB-backed)"
        )


def _db_enabled() -> bool:
    url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    return bool(url and url.strip())


def _is_test_isolation() -> bool:
    if os.environ.get("OAOS_ENV", "").lower() in ("production", "prod"):
        return False
    if os.environ.get("OAOS_APPROVAL_FORCE_DB", "").lower() in ("1", "true", "yes"):
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if os.environ.get("OAOS_ENV", "").lower() in ("test", "testing"):
        return True
    return False


def _db_should_use() -> bool:
    return _db_enabled() and not _is_test_isolation()


def _normalize_sync_url(url: str) -> str:
    u = url.strip()
    if "+asyncpg" in u:
        u = u.replace("+asyncpg", "")
    if "+aiosqlite" in u:
        u = u.replace("+aiosqlite", "")
    if u.startswith("postgresql+asyncpg://"):
        u = u.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if u.startswith("sqlite+aiosqlite://"):
        u = u.replace("sqlite+aiosqlite://", "sqlite://", 1)
    # ensure postgres uses psycopg (psycopg[binary] / psycopg3) driver
    if u.startswith("postgresql://"):
        u = u.replace("postgresql://", "postgresql+psycopg://", 1)
    return u


def _db_sync_url() -> str | None:
    url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url or not url.strip():
        return None
    return _normalize_sync_url(url.strip())


def _db_get_session():
    url = _db_sync_url()
    if not url:
        return None, None
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
    except (ImportError, ModuleNotFoundError):
        return None, None
    try:
        connect_args = {}
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        engine = create_engine(url, echo=False, pool_pre_ping=False, connect_args=connect_args)
        try:
            from security.models.db import Base  # type: ignore
            from security.models.orm import ApprovalRequestORM, ApprovalNonceORM  # noqa: F401  # type: ignore
            Base.metadata.create_all(bind=engine)
        except (ImportError, ModuleNotFoundError, SQLAlchemyError):
            try:
                import sys
                from pathlib import Path

                sec = Path(__file__).resolve().parents[2]
                if str(sec) not in sys.path:
                    sys.path.insert(0, str(sec))
                from security.models.db import Base  # type: ignore
                from security.models.orm import ApprovalRequestORM, ApprovalNonceORM  # noqa: F401  # type: ignore
                Base.metadata.create_all(bind=engine)
            except (ImportError, ModuleNotFoundError) as e:
                logger.debug("approval ORM import fallback unavailable: %s", type(e).__name__)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()
        return session, engine
    except (ImportError, OSError, RuntimeError, SQLAlchemyError, TypeError, ValueError) as e:
        # Do not log the raw DB exception: SQLAlchemy errors can include a
        # rendered URL or bound values (including credentials).
        logger.debug("ApprovalStore DB session failed: %s", type(e).__name__)
        return None, None


def _db_close(session, engine) -> None:
    try:
        if session is not None:
            session.close()
    except SQLAlchemyError:
        logger.debug("approval session close failed (best-effort)")
    try:
        if engine is not None:
            engine.dispose()
    except SQLAlchemyError:
        logger.debug("approval engine dispose failed (best-effort)")


# ── in-memory fallback dict with set-compatible .add ───────────
class _SeenNonces(dict):  # type: ignore
    """dict nonce->expires_at with .add for set-compat (old code used set)."""

    def add(self, nonce: str) -> None:  # set-like
        self[nonce] = datetime.now(timezone.utc) + timedelta(seconds=NONCE_TTL_SECONDS)
        try:
            if not _db_nonce_insert(nonce):
                logger.warning("approval nonce persistence unavailable; using non-production memory fallback")
        except (ImportError, OSError, RuntimeError, SQLAlchemyError, TypeError, ValueError) as e:
            logger.warning("approval nonce persistence failed: %s", type(e).__name__)

    def discard(self, nonce: str) -> None:
        self.pop(nonce, None)


# ── approval_nonces helpers (DB + psycopg + in-memory fallback) ──

def _db_nonce_cleanup(session) -> int:
    """Delete expired nonces (TTL 300s). Returns deleted count. No-throw."""
    try:
        from security.models.orm import ApprovalNonceORM  # type: ignore

        now = datetime.now(timezone.utc)
        deleted = session.query(ApprovalNonceORM).filter(ApprovalNonceORM.expires_at < now).delete()  # type: ignore
        try:
            session.commit()
        except SQLAlchemyError as e:
            logger.debug("approval nonce commit failed: %s", type(e).__name__)
            try:
                session.rollback()
            except SQLAlchemyError:
                logger.debug("approval nonce rollback failed (best-effort)")
        return int(deleted or 0)
    except (ImportError, AttributeError, SQLAlchemyError, TypeError, ValueError) as e:
        logger.debug("approval nonce cleanup failed: %s", type(e).__name__)
        return 0


def _db_nonce_exists(nonce: str) -> bool:
    """Check if nonce exists in DB (with TTL cleanup). Falls back to False on DB error."""
    if not _db_should_use():
        return False
    session, engine = _db_get_session()
    if session is None:
        return False
    try:
        # opportunistic GC
        _db_nonce_cleanup(session)
        try:
            from security.models.orm import ApprovalNonceORM  # type: ignore

            row = session.query(ApprovalNonceORM).filter(ApprovalNonceORM.nonce == nonce).first()  # type: ignore
            return row is not None
        except (ImportError, AttributeError, SQLAlchemyError, TypeError, ValueError) as e:
            logger.debug("nonce DB exists check failed: %s", type(e).__name__)
            return False
    finally:
        _db_close(session, engine)


def _db_nonce_insert(nonce: str, expires_at: datetime | None = None) -> bool:
    """Insert nonce into approval_nonces with TTL.

    Returns ``True`` only when this call inserted the nonce.  A pre-existing
    row or a unique collision returns ``False`` so callers cannot treat a
    replay as a newly claimed nonce.  The durable decision path below uses its
    own transaction to surface that collision as ``nonce replay detected``.

    Uses postgresql+psycopg via SQLAlchemy when DATABASE_URL is postgres,
    sqlite for tests. On DB error returns False (caller falls back to in-memory).
    """
    if not _db_should_use():
        return False
    session, engine = _db_get_session()
    if session is None:
        return False
    try:
        # GC expired first
        _db_nonce_cleanup(session)
        from security.models.orm import ApprovalNonceORM  # type: ignore

        # A pre-existing row is a replay, not a successful claim.
        try:
            existing = session.query(ApprovalNonceORM).filter(ApprovalNonceORM.nonce == nonce).first()  # type: ignore
            if existing is not None:
                return False
        except (ImportError, AttributeError, SQLAlchemyError, TypeError, ValueError) as e:
            logger.debug("nonce deduplication query failed: %s", type(e).__name__)
        now = datetime.now(timezone.utc)
        exp = expires_at
        if exp is None:
            exp = now + timedelta(seconds=NONCE_TTL_SECONDS)
        else:
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            # cap to TTL from now if request expiry is longer (replay window = 300s)
            ttl_exp = now + timedelta(seconds=NONCE_TTL_SECONDS)
            # keep the earlier of the two so GC is correct (use min)
            if exp > ttl_exp:
                exp = ttl_exp
        row = ApprovalNonceORM(nonce=nonce, created_at=now, expires_at=exp)
        session.add(row)
        session.commit()
        return True
    except IntegrityError:
        _rollback_quietly(session)
        logger.debug("nonce DB insert rejected: unique constraint")
        return False
    except (ImportError, AttributeError, SQLAlchemyError, TypeError, ValueError) as e:
        _rollback_quietly(session)
        logger.debug("nonce DB insert failed: %s", type(e).__name__)
        return False
    finally:
        _db_close(session, engine)


def _to_orm(req: ApprovalRequest):
    try:
        from security.models.orm import ApprovalRequestORM  # type: ignore
    except ImportError:
        import sys
        from pathlib import Path

        sec = Path(__file__).resolve().parents[2]
        if str(sec) not in sys.path:
            sys.path.insert(0, str(sec))
        from security.models.orm import ApprovalRequestORM  # type: ignore
    return ApprovalRequestORM(
        approval_id=req.approval_id,
        user_id=req.user_id,
        agent_id=req.agent_id,
        resource=req.resource,
        action=req.action,
        risk=req.risk,
        request_hash=req.request_hash,
        nonce=req.nonce,
        expires_at=req.expires_at,
        signature=req.signature,
        decision=req.decision.value if hasattr(req.decision, "value") else str(req.decision),
        decided_at=req.decided_at,
        decided_by=req.decided_by,
        group_id=None,
        created_at=datetime.now(timezone.utc),
    )


def _from_orm(row) -> ApprovalRequest:
    dec_val = getattr(row, "decision", "PENDING")
    try:
        dec = ApprovalDecision(dec_val)
    except (TypeError, ValueError) as e:
        logger.warning("invalid persisted approval decision: %s", type(e).__name__)
        raise ValueError("invalid persisted approval decision") from e
    expires_at = getattr(row, "expires_at")
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    decided_at = getattr(row, "decided_at", None)
    if decided_at is not None and decided_at.tzinfo is None:
        decided_at = decided_at.replace(tzinfo=timezone.utc)
    return ApprovalRequest(
        approval_id=str(row.approval_id),
        user_id=str(row.user_id),
        agent_id=str(row.agent_id),
        resource=str(row.resource),
        action=str(row.action),
        risk=str(getattr(row, "risk", "HIGH")),
        request_hash=str(row.request_hash),
        nonce=str(row.nonce),
        expires_at=expires_at,
        signature=getattr(row, "signature", None),
        decision=dec,
        decided_at=decided_at,
        decided_by=getattr(row, "decided_by", None),
    )


def _rollback_quietly(session) -> None:
    """Roll back a failed approval transaction without leaking DB details."""
    try:
        session.rollback()
    except (AttributeError, OSError, RuntimeError, SQLAlchemyError, TypeError):
        logger.debug("approval transaction rollback failed (best-effort)")


def _nonce_expiry(now: datetime, expires_at: datetime | None) -> datetime:
    """Return the nonce expiry, bounded by the replay-protection TTL."""
    exp = expires_at
    if exp is None:
        exp = now + timedelta(seconds=NONCE_TTL_SECONDS)
    elif exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    ttl_exp = now + timedelta(seconds=NONCE_TTL_SECONDS)
    return min(exp, ttl_exp)


def _db_decision_transaction(
    approval_id: str,
    nonce: str,
    decision: ApprovalDecision,
    decided_by: str,
    group_id: str | None,
) -> datetime | None:
    """Claim a nonce and finalize an approval in one database transaction.

    ``None`` means that a DB session could not be opened, allowing the caller
    to retain the non-production in-memory fallback.  Once a session exists,
    all failures are surfaced and the caller must not mutate memory.
    """
    session, engine = _db_get_session()
    if session is None:
        return None

    try:
        from security.models.orm import ApprovalNonceORM, ApprovalRequestORM  # type: ignore

        now = datetime.now(timezone.utc)
        row = (
            session.query(ApprovalRequestORM)
            .filter(ApprovalRequestORM.approval_id == approval_id)
            .first()
        )
        if row is None:
            raise KeyError(f"approval not found: {approval_id}")

        # Re-read all state in this transaction.  The request object supplied
        # by the caller may have been cached before another worker decided it.
        if not hmac.compare_digest(str(row.nonce), nonce):
            raise ValueError("approval nonce mismatch")
        persisted_decision = str(getattr(row, "decision", ApprovalDecision.PENDING.value))
        if persisted_decision != ApprovalDecision.PENDING.value:
            raise ValueError(f"already decided: {persisted_decision}")
        persisted_expiry = getattr(row, "expires_at", None)
        if persisted_expiry is not None and persisted_expiry.tzinfo is None:
            persisted_expiry = persisted_expiry.replace(tzinfo=timezone.utc)
        if persisted_expiry is not None and persisted_expiry < now:
            raise ValueError("approval expired")

        # Cleanup is deliberately part of this transaction.  A separate
        # cleanup commit would break the nonce claim + decision atomicity.
        session.query(ApprovalNonceORM).filter(ApprovalNonceORM.expires_at < now).delete(
            synchronize_session=False
        )
        session.add(
            ApprovalNonceORM(
                nonce=nonce,
                created_at=now,
                expires_at=_nonce_expiry(now, persisted_expiry),
            )
        )
        try:
            # Flush now so a unique nonce collision is rejected before the
            # approval update and before any commit can make a partial claim.
            session.flush()
        except IntegrityError:
            _rollback_quietly(session)
            logger.debug("approval nonce claim rejected: unique constraint")
            raise ValueError("nonce replay detected") from None

        decided_at = now
        values = {
            "decision": decision.value,
            "decided_at": decided_at,
            "decided_by": decided_by,
        }
        if group_id is not None:
            values["group_id"] = group_id

        # The nonce claim above and this compare-and-set update share one
        # commit.  The nonce TTL may have elapsed, but a final decision can
        # never be overwritten because this predicate still requires PENDING.
        affected = (
            session.query(ApprovalRequestORM)
            .filter(
                ApprovalRequestORM.approval_id == approval_id,
                ApprovalRequestORM.nonce == nonce,
                ApprovalRequestORM.decision == ApprovalDecision.PENDING.value,
            )
            .update(values, synchronize_session=False)
        )
        if int(affected or 0) != 1:
            _rollback_quietly(session)
            raise ValueError("approval already decided")

        session.commit()
        return decided_at
    except (KeyError, ValueError):
        _rollback_quietly(session)
        raise
    except (AttributeError, ImportError, OSError, RuntimeError, SQLAlchemyError, TypeError) as e:
        _rollback_quietly(session)
        # SQLAlchemy exceptions can contain rendered URLs and bound values.
        # Keep logs and public errors free of those details.
        logger.debug("approval decision transaction failed: %s", type(e).__name__)
        raise RuntimeError("ApprovalStore decision persistence failed") from None
    finally:
        _db_close(session, engine)


class ApprovalStore:
    """In-memory approval lifecycle 관리 with optional DB persistence.

    Nonce replay protection persists to approval_nonces (DB) with TTL 300s.
    Falls back to in-memory dict when DB unavailable (e.g. tests / no DATABASE_URL).
    verify replay survives restart via DB query.

    Distributed/operational boundary:
    - Production (OAOS_ENV=production): DB/Redis is primary, in-memory fallback is
      NOT allowed — fail-closed (raise) if DB unavailable. Limitations documented
      in file header: in-memory is process-local, not distributed.
    - Non-prod: explicit test fallback via in-memory is retained.
    """

    def __init__(self, signing_key: str) -> None:
        self.signing_key = signing_key
        self._requests: dict[str, ApprovalRequest] = {}
        # in-memory fallback: nonce -> expires_at (dict with .add for set compat)
        self._seen_nonces: _SeenNonces = _SeenNonces()
        # backwards compat: expose set-like view for older callers that do `in` checks
        self._user_grants: set[tuple[str, str, str]] = set()  # (user_id, action, resource_pattern)
        self._group_grants: set[tuple[str, str, str]] = set()  # (group_id, action, resource_pattern)
        if _is_prod():
            try:
                _require_db_if_prod()
            except RuntimeError:
                raise

    # ── internal nonce helpers ────────────────────────────────
    def _purge_expired_nonces(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [k for k, exp in list(self._seen_nonces.items()) if exp < now]
        for k in expired:
            self._seen_nonces.pop(k, None)

    def _is_nonce_seen(self, nonce: str) -> bool:
        # check in-memory (with TTL)
        self._purge_expired_nonces()
        if nonce in self._seen_nonces:
            return True
        # check DB (survives restart)
        if _db_should_use() and _db_nonce_exists(nonce):
            # hydrate in-memory for faster next check
            self._seen_nonces[nonce] = datetime.now(timezone.utc) + timedelta(seconds=NONCE_TTL_SECONDS)
            return True
        return False

    def _mark_nonce_seen(self, nonce: str, expires_at: datetime | None = None) -> None:
        exp = expires_at
        if exp is None:
            exp = datetime.now(timezone.utc) + timedelta(seconds=NONCE_TTL_SECONDS)
        else:
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            # cap to TTL
            ttl_exp = datetime.now(timezone.utc) + timedelta(seconds=NONCE_TTL_SECONDS)
            if exp > ttl_exp:
                exp = ttl_exp
        # persist to DB — primary in prod (fail-closed), fallback in non-prod
        if _db_should_use():
            persisted = _db_nonce_insert(nonce, exp)
            if not persisted:
                if _is_prod():
                    raise RuntimeError("ApprovalStore nonce persist failed in production")
                logger.warning("ApprovalStore nonce persist failed; retaining non-production memory fallback")
        else:
            if _is_prod():
                raise RuntimeError("ApprovalStore nonce persist failed — no DB in production (fail-closed)")
        self._seen_nonces[nonce] = exp

    # ── 생성 ───────────────────────────────────────────────────
    def create(
        self,
        user_id: str,
        agent_id: str,
        action: str,
        resource: str,
        risk: str = "HIGH",
        ttl_minutes: int = 60,
    ) -> ApprovalRequest:
        if _is_prod():
            _require_db_if_prod()
        nonce = uuid.uuid4().hex
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        raw = f"{user_id}|{agent_id}|{action}|{resource}|{nonce}|{expires_at.isoformat()}"
        request_hash = hashlib.sha256(raw.encode()).hexdigest()
        sig = hmac.new(self.signing_key.encode(), request_hash.encode(), hashlib.sha256).hexdigest()
        req = ApprovalRequest(
            approval_id=f"apr_{uuid.uuid4().hex[:12]}",
            user_id=user_id,
            agent_id=agent_id,
            resource=resource,
            action=action,
            risk=risk,
            request_hash=request_hash,
            nonce=nonce,
            expires_at=expires_at,
            signature=sig,
        )
        self._requests[req.approval_id] = req
        if _db_should_use():
            db_ok = False
            try:
                session, engine = _db_get_session()
                if session is None:
                    if _is_prod():
                        raise RuntimeError("ApprovalStore create DB unavailable in production")
                else:
                    try:
                        orm = _to_orm(req)
                        session.add(orm)
                        session.commit()
                        db_ok = True
                    except (ImportError, OSError, RuntimeError, SQLAlchemyError, TypeError, ValueError) as e:
                        _rollback_quietly(session)
                        logger.debug("ApprovalStore create DB persist failed: %s", type(e).__name__)
                    finally:
                        _db_close(session, engine)
            except (ImportError, OSError, RuntimeError, SQLAlchemyError, TypeError, ValueError):
                pass
            if _is_prod() and not db_ok:
                self._requests.pop(req.approval_id, None)
                raise RuntimeError("ApprovalStore create failed — DB persist required in production") from None
        elif _is_prod():
            self._requests.pop(req.approval_id, None)
            raise RuntimeError("ApprovalStore create failed — no DB in production (fail-closed)")
        return req

    def get(self, approval_id: str) -> ApprovalRequest | None:
        if _db_should_use():
            try:
                session, engine = _db_get_session()
                if session is None:
                    if _is_prod():
                        raise RuntimeError("ApprovalStore get DB unavailable in production")
                else:
                    try:
                        from security.models.orm import ApprovalRequestORM  # type: ignore

                        row = session.query(ApprovalRequestORM).filter(ApprovalRequestORM.approval_id == approval_id).first()  # type: ignore
                        if row is not None:
                            req = _from_orm(row)
                            self._requests[req.approval_id] = req
                            # hydrate grants if decision indicates
                            if req.decision == ApprovalDecision.APPROVED_USER_ALWAYS:
                                self._user_grants.add((req.user_id, req.action, req.resource))
                            elif req.decision == ApprovalDecision.APPROVED_GROUP_ALWAYS:
                                gid = getattr(row, "group_id", None)
                                if gid:
                                    self._group_grants.add((str(gid), req.action, req.resource))
                            return req
                        # A successful DB read is authoritative even in
                        # non-production.  Do not resurrect a stale in-memory
                        # request when the row was deleted elsewhere.
                        self._requests.pop(approval_id, None)
                        return None
                    finally:
                        _db_close(session, engine)
            except (ImportError, OSError, RuntimeError, SQLAlchemyError, TypeError, ValueError) as e:
                if _is_prod():
                    raise RuntimeError("ApprovalStore get failed — DB unavailable in production") from None
                logger.warning("ApprovalStore get DB fallback to memory: %s", type(e).__name__)
        return self._requests.get(approval_id)

    # ── 검증 ───────────────────────────────────────────────────
    def verify(self, req: ApprovalRequest) -> bool:
        """signature + nonce + expiry + hash 검증."""
        # expiry
        if req.expires_at < datetime.now(timezone.utc):
            return False
        # signature
        expected_sig = hmac.new(
            self.signing_key.encode(), req.request_hash.encode(), hashlib.sha256
        ).hexdigest()
        if req.signature != expected_sig:
            return False
        # request_hash 재계산 검증
        raw = f"{req.user_id}|{req.agent_id}|{req.action}|{req.resource}|{req.nonce}|{req.expires_at.isoformat()}"
        expected_hash = hashlib.sha256(raw.encode()).hexdigest()
        if req.request_hash != expected_hash:
            return False
        return True

    def is_expired(self, approval_id: str) -> bool:
        req = self.get(approval_id)
        if req is None:
            return True
        return req.expires_at < datetime.now(timezone.utc)

    # ── 결정 (4 decisions) ─────────────────────────────────────
    def decide(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        decided_by: str,
        group_id: str | None = None,
    ) -> ApprovalRequest:
        try:
            decision = ApprovalDecision(decision)
        except (TypeError, ValueError) as e:
            raise ValueError("invalid approval decision") from e
        req = self.get(approval_id)
        if req is None:
            raise KeyError(f"approval not found: {approval_id}")
        if req.expires_at < datetime.now(timezone.utc):
            raise ValueError("approval expired")
        if req.decision != ApprovalDecision.PENDING:
            raise ValueError(f"already decided: {req.decision}")
        if decision == ApprovalDecision.PENDING:
            raise ValueError("final approval decision required")
        if decision == ApprovalDecision.APPROVED_GROUP_ALWAYS and not group_id:
            raise ValueError("group_id required for group-always")

        decided_at: datetime
        persisted_at: datetime | None = None
        if _db_should_use():
            try:
                persisted_at = _db_decision_transaction(
                    approval_id=approval_id,
                    nonce=req.nonce,
                    decision=decision,
                    decided_by=decided_by,
                    group_id=group_id,
                )
            except (KeyError, ValueError):
                # The transaction rolled back.  Leave the cached request and
                # grants untouched so a retry can make the same decision.
                raise
            except RuntimeError:
                if _is_prod():
                    raise RuntimeError(
                        "ApprovalStore decide failed — DB persist required in production"
                    ) from None
                raise

            if persisted_at is None:
                if _is_prod():
                    raise RuntimeError(
                        "ApprovalStore decide failed — DB persist required in production"
                    )
                # The configured DB could not be opened.  Keep the existing
                # non-production memory fallback, including TTL replay checks.
                if self._is_nonce_seen(req.nonce):
                    raise ValueError("nonce replay detected")
                self._mark_nonce_seen(req.nonce, req.expires_at)
                decided_at = datetime.now(timezone.utc)
            else:
                decided_at = persisted_at
        else:
            if _is_prod():
                raise RuntimeError("ApprovalStore decide failed — no DB in production (fail-closed)")
            # Non-production, no-DB mode remains intentionally process-local.
            if self._is_nonce_seen(req.nonce):
                raise ValueError("nonce replay detected")
            self._mark_nonce_seen(req.nonce, req.expires_at)
            decided_at = datetime.now(timezone.utc)

        # A durable decision is visible in memory only after the transaction
        # committed.  This also keeps an injected commit failure retryable.
        if _db_should_use() and persisted_at is not None:
            self._seen_nonces[req.nonce] = _nonce_expiry(decided_at, req.expires_at)
        req.decision = decision
        req.decided_at = decided_at
        req.decided_by = decided_by
        if decision == ApprovalDecision.APPROVED_USER_ALWAYS:
            self._user_grants.add((req.user_id, req.action, req.resource))
        elif decision == ApprovalDecision.APPROVED_GROUP_ALWAYS:
            self._group_grants.add((group_id, req.action, req.resource))
        self._requests[req.approval_id] = req
        return req

    def is_approved(self, approval_id: str) -> bool:
        req = self.get(approval_id)
        if req is None:
            return False
        return req.decision in (
            ApprovalDecision.APPROVED_ONCE,
            ApprovalDecision.APPROVED_USER_ALWAYS,
            ApprovalDecision.APPROVED_GROUP_ALWAYS,
        )

    def has_user_grant(self, user_id: str, action: str, resource: str) -> bool:
        import fnmatch

        # A successful DB query replaces the cache so revocations/updates made
        # by another worker are observed on every lookup.  Keep the old cache
        # only when the DB could not be read and non-prod fallback is allowed.
        if _db_should_use():
            try:
                session, engine = _db_get_session()
                if session is None:
                    if _is_prod():
                        raise RuntimeError("ApprovalStore user-grant DB unavailable in production")
                else:
                    try:
                        from security.models.orm import ApprovalRequestORM  # type: ignore

                        rows = session.query(ApprovalRequestORM).filter(ApprovalRequestORM.decision == "APPROVED_USER_ALWAYS").all()  # type: ignore
                        refreshed: set[tuple[str, str, str]] = set()
                        for r in rows:
                            refreshed.add((str(r.user_id), str(r.action), str(r.resource)))
                        self._user_grants.clear()
                        self._user_grants.update(refreshed)
                    finally:
                        _db_close(session, engine)
            except (ImportError, OSError, RuntimeError, SQLAlchemyError, TypeError, ValueError) as e:
                if _is_prod():
                    raise RuntimeError("ApprovalStore user-grant lookup failed in production") from None
                # Grant hydrate fallback to memory-only (deny-direction: fewer grants => deny)
                logger.warning("ApprovalStore user-grant hydrate failed, memory-only: %s", type(e).__name__)
        for (u, a, pattern) in self._user_grants:
            if u == user_id and a == action and fnmatch.fnmatch(resource, pattern):
                return True
        return False

    def has_group_grant(self, group_id: str, action: str, resource: str) -> bool:
        import fnmatch

        if _db_should_use():
            try:
                session, engine = _db_get_session()
                if session is None:
                    if _is_prod():
                        raise RuntimeError("ApprovalStore group-grant DB unavailable in production")
                else:
                    try:
                        from security.models.orm import ApprovalRequestORM  # type: ignore

                        rows = session.query(ApprovalRequestORM).filter(ApprovalRequestORM.decision == "APPROVED_GROUP_ALWAYS").all()  # type: ignore
                        refreshed: set[tuple[str, str, str]] = set()
                        for r in rows:
                            gid = getattr(r, "group_id", None)
                            if gid:
                                refreshed.add((str(gid), str(r.action), str(r.resource)))
                        self._group_grants.clear()
                        self._group_grants.update(refreshed)
                    finally:
                        _db_close(session, engine)
            except (ImportError, OSError, RuntimeError, SQLAlchemyError, TypeError, ValueError) as e:
                if _is_prod():
                    raise RuntimeError("ApprovalStore group-grant lookup failed in production") from None
                # Grant hydrate fallback to memory-only (deny-direction: fewer grants => deny)
                logger.warning("ApprovalStore group-grant hydrate failed, memory-only: %s", type(e).__name__)
        for (g, a, pattern) in self._group_grants:
            if g == group_id and a == action and fnmatch.fnmatch(resource, pattern):
                return True
        return False

    # ── explicit nonce GC (callable externally / cron) ────────
    def cleanup_expired_nonces(self) -> int:
        """Purge expired nonces from memory and DB (TTL 300s). Returns total removed."""
        removed = 0
        before = len(self._seen_nonces)
        self._purge_expired_nonces()
        removed += before - len(self._seen_nonces)
        if _db_should_use():
            session, engine = _db_get_session()
            if session is not None:
                try:
                    removed += _db_nonce_cleanup(session)
                finally:
                    _db_close(session, engine)
        return removed


# ── 모듈 레벨 헬퍼 (기존 import 호환) ───────────────────────────


def create_approval_request(
    signing_key: str,
    user_id: str,
    agent_id: str,
    action: str,
    resource: str,
    risk: str = "HIGH",
    ttl_minutes: int = 60,
) -> ApprovalRequest:
    """Stateless helper — ApprovalStore 없이 단건 생성."""
    nonce = uuid.uuid4().hex
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    raw = f"{user_id}|{agent_id}|{action}|{resource}|{nonce}|{expires_at.isoformat()}"
    request_hash = hashlib.sha256(raw.encode()).hexdigest()
    sig = hmac.new(signing_key.encode(), request_hash.encode(), hashlib.sha256).hexdigest()
    return ApprovalRequest(
        approval_id=f"apr_{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        agent_id=agent_id,
        resource=resource,
        action=action,
        risk=risk,
        request_hash=request_hash,
        nonce=nonce,
        expires_at=expires_at,
        signature=sig,
    )


def verify_approval_request(signing_key: str, req: ApprovalRequest) -> bool:
    """Stateless 검증 helper."""
    if req.expires_at < datetime.now(timezone.utc):
        return False
    expected_sig = hmac.new(
        signing_key.encode(), req.request_hash.encode(), hashlib.sha256
    ).hexdigest()
    if req.signature != expected_sig:
        return False
    raw = f"{req.user_id}|{req.agent_id}|{req.action}|{req.resource}|{req.nonce}|{req.expires_at.isoformat()}"
    expected_hash = hashlib.sha256(raw.encode()).hexdigest()
    return req.request_hash == expected_hash
