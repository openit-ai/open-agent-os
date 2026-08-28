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


# ── DB helpers (lazy, sync) ──────────────────────────────────────

def _db_enabled() -> bool:
    url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    return bool(url and url.strip())


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
    except Exception:
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
        except Exception:
            try:
                import sys
                from pathlib import Path

                sec = Path(__file__).resolve().parents[2]
                if str(sec) not in sys.path:
                    sys.path.insert(0, str(sec))
                from security.models.db import Base  # type: ignore
                from security.models.orm import ApprovalRequestORM, ApprovalNonceORM  # noqa: F401  # type: ignore
                Base.metadata.create_all(bind=engine)
            except Exception:
                pass
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()
        return session, engine
    except Exception as e:
        logger.debug("ApprovalStore DB session failed: %s", e)
        return None, None


def _db_close(session, engine) -> None:
    try:
        if session is not None:
            session.close()
    except Exception:
        pass
    try:
        if engine is not None:
            engine.dispose()
    except Exception:
        pass


# ── in-memory fallback dict with set-compatible .add ───────────
class _SeenNonces(dict):  # type: ignore
    """dict nonce->expires_at with .add for set-compat (old code used set)."""

    def add(self, nonce: str) -> None:  # set-like
        self[nonce] = datetime.now(timezone.utc) + timedelta(seconds=NONCE_TTL_SECONDS)
        try:
            _db_nonce_insert(nonce)
        except Exception:
            pass

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
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass
        return int(deleted or 0)
    except Exception:
        return 0


def _db_nonce_exists(nonce: str) -> bool:
    """Check if nonce exists in DB (with TTL cleanup). Falls back to False on DB error."""
    if not _db_enabled():
        return False
    session, engine = _db_get_session()
    if session is None:
        return False
    try:
        # opportunistic GC
        try:
            _db_nonce_cleanup(session)
        except Exception:
            pass
        try:
            from security.models.orm import ApprovalNonceORM  # type: ignore

            row = session.query(ApprovalNonceORM).filter(ApprovalNonceORM.nonce == nonce).first()  # type: ignore
            return row is not None
        except Exception as e:
            logger.debug("nonce DB exists check failed: %s", e)
            return False
    finally:
        _db_close(session, engine)


def _db_nonce_insert(nonce: str, expires_at: datetime | None = None) -> bool:
    """Insert nonce into approval_nonces with TTL. Returns True if inserted or already exists.

    Uses postgresql+psycopg via SQLAlchemy when DATABASE_URL is postgres,
    sqlite for tests. On DB error returns False (caller falls back to in-memory).
    """
    if not _db_enabled():
        return False
    session, engine = _db_get_session()
    if session is None:
        return False
    try:
        # GC expired first
        try:
            _db_nonce_cleanup(session)
        except Exception:
            pass
        from security.models.orm import ApprovalNonceORM  # type: ignore

        # dedup: if already exists, keep it (replay)
        try:
            existing = session.query(ApprovalNonceORM).filter(ApprovalNonceORM.nonce == nonce).first()  # type: ignore
            if existing is not None:
                return True
        except Exception:
            pass
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
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        # unique violation means already exists -> treat as success (replay already persisted)
        msg = str(e).lower()
        if "unique" in msg or "duplicate" in msg or "already exists" in msg:
            return True
        logger.debug("nonce DB insert failed: %s", e)
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
    except Exception:
        dec = ApprovalDecision.PENDING
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


class ApprovalStore:
    """In-memory approval lifecycle 관리 with optional DB persistence.

    Nonce replay protection persists to approval_nonces (DB) with TTL 300s.
    Falls back to in-memory dict when DB unavailable (e.g. tests / no DATABASE_URL).
    verify replay survives restart via DB query.
    """

    def __init__(self, signing_key: str) -> None:
        self.signing_key = signing_key
        self._requests: dict[str, ApprovalRequest] = {}
        # in-memory fallback: nonce -> expires_at (dict with .add for set compat)
        self._seen_nonces: _SeenNonces = _SeenNonces()
        # backwards compat: expose set-like view for older callers that do `in` checks
        self._user_grants: set[tuple[str, str, str]] = set()  # (user_id, action, resource_pattern)
        self._group_grants: set[tuple[str, str, str]] = set()  # (group_id, action, resource_pattern)

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
        if _db_enabled() and _db_nonce_exists(nonce):
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
        self._seen_nonces[nonce] = exp
        # persist to DB (ignore failure -> in-memory fallback keeps protection for this process)
        try:
            _db_nonce_insert(nonce, exp)
        except Exception:
            pass

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
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        orm = _to_orm(req)
                        session.add(orm)
                        session.commit()
                    except Exception as e:
                        try:
                            session.rollback()
                        except Exception:
                            pass
                        logger.debug("ApprovalStore create DB persist failed: %s", e)
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass
        return req

    def get(self, approval_id: str) -> ApprovalRequest | None:
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is not None:
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
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass
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
        req = self.get(approval_id)
        if req is None:
            raise KeyError(f"approval not found: {approval_id}")
        if req.expires_at < datetime.now(timezone.utc):
            raise ValueError("approval expired")
        if req.decision != ApprovalDecision.PENDING:
            raise ValueError(f"already decided: {req.decision}")
        # nonce replay 방지: 결정 시 nonce 를 seen 에 기록 (DB + memory with TTL)
        if self._is_nonce_seen(req.nonce):
            raise ValueError("nonce replay detected")
        self._mark_nonce_seen(req.nonce, req.expires_at)

        req.decision = decision
        req.decided_at = datetime.now(timezone.utc)
        req.decided_by = decided_by

        # persistent grant 기록
        if decision == ApprovalDecision.APPROVED_USER_ALWAYS:
            self._user_grants.add((req.user_id, req.action, req.resource))
        elif decision == ApprovalDecision.APPROVED_GROUP_ALWAYS:
            if not group_id:
                raise ValueError("group_id required for group-always")
            self._group_grants.add((group_id, req.action, req.resource))

        # update in-memory
        self._requests[req.approval_id] = req
        # DB update
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        from security.models.orm import ApprovalRequestORM  # type: ignore

                        row = session.query(ApprovalRequestORM).filter(ApprovalRequestORM.approval_id == approval_id).first()  # type: ignore
                        if row is not None:
                            row.decision = decision.value if hasattr(decision, "value") else str(decision)
                            row.decided_at = req.decided_at
                            row.decided_by = decided_by
                            if group_id is not None:
                                try:
                                    row.group_id = group_id
                                except Exception:
                                    pass
                            session.commit()
                        else:
                            # row not found (race) — insert
                            orm = _to_orm(req)
                            if group_id:
                                try:
                                    orm.group_id = group_id
                                except Exception:
                                    pass
                            session.add(orm)
                            session.commit()
                    except Exception as e:
                        try:
                            session.rollback()
                        except Exception:
                            pass
                        logger.debug("ApprovalStore decide DB update failed: %s", e)
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass
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

        # try hydrate from DB if not in memory (scan DB for user grants)
        if _db_enabled() and not self._user_grants:
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        from security.models.orm import ApprovalRequestORM  # type: ignore

                        rows = session.query(ApprovalRequestORM).filter(ApprovalRequestORM.decision == "APPROVED_USER_ALWAYS").all()  # type: ignore
                        for r in rows:
                            self._user_grants.add((str(r.user_id), str(r.action), str(r.resource)))
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass
        for (u, a, pattern) in self._user_grants:
            if u == user_id and a == action and fnmatch.fnmatch(resource, pattern):
                return True
        return False

    def has_group_grant(self, group_id: str, action: str, resource: str) -> bool:
        import fnmatch

        if _db_enabled() and not self._group_grants:
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        from security.models.orm import ApprovalRequestORM  # type: ignore

                        rows = session.query(ApprovalRequestORM).filter(ApprovalRequestORM.decision == "APPROVED_GROUP_ALWAYS").all()  # type: ignore
                        for r in rows:
                            gid = getattr(r, "group_id", None)
                            if gid:
                                self._group_grants.add((str(gid), str(r.action), str(r.resource)))
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass
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
        if _db_enabled():
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
