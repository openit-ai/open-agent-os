"""Hash-chain Audit Ledger — Section 31.
- hash(previous_hash + canonical_payload)
- checkpoint sign with HMAC (Section 31 주기적 서명)
- verify_chain (무결성 검증)
- DB persistence: when DATABASE_URL/OAOS_DATABASE_URL is set, audit_events
  are persisted to audit_events table (sync SQLAlchemy, sqlite compat).
  Falls back to in-memory list. All DB imports are lazy.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
import os
import logging
from datetime import datetime, timezone
from typing import Optional

from audit_model import AuditCheckpoint, AuditEvent


logger = logging.getLogger(__name__)

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
        u = u.replace("postgresql+asyncpg://", "postgresql://", 1)
    if u.startswith("sqlite+aiosqlite://"):
        u = u.replace("sqlite+aiosqlite://", "sqlite://", 1)
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
            from security.models.orm import AuditEventORM  # noqa: F401  # type: ignore
            Base.metadata.create_all(bind=engine)
        except Exception:
            try:
                import sys
                from pathlib import Path
                sec = Path(__file__).resolve().parents[2]
                if str(sec) not in sys.path:
                    sys.path.insert(0, str(sec))
                from security.models.db import Base  # type: ignore
                from security.models.orm import AuditEventORM  # noqa: F401  # type: ignore
                Base.metadata.create_all(bind=engine)
            except Exception:
                pass
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()
        return session, engine
    except Exception as e:
        logger.debug("AuditLedger DB session failed: %s", e)
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


def _event_to_orm(event: AuditEvent):
    try:
        from security.models.orm import AuditEventORM  # type: ignore
    except ImportError:
        import sys
        from pathlib import Path
        sec = Path(__file__).resolve().parents[2]
        if str(sec) not in sys.path:
            sys.path.insert(0, str(sec))
        from security.models.orm import AuditEventORM  # type: ignore
    return AuditEventORM(
        event_id=event.event_id,
        event_type=event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type),
        timestamp=event.timestamp,
        tenant_id=event.tenant_id,
        user_id=event.user_id,
        agent_id=event.agent_id,
        session_id=event.session_id,
        trace_id=event.trace_id,
        request_id=event.request_id,
        resource=event.resource,
        action=event.action,
        decision=event.decision,
        policy_version=event.policy_version,
        delegation_id=event.delegation_id,
        credential_binding_id=event.credential_binding_id,
        tool_name=event.tool_name,
        parameters_hash=event.parameters_hash,
        result_hash=event.result_hash,
        previous_hash=event.previous_hash,
        event_hash=event.event_hash,
    )


def _orm_to_event(row) -> AuditEvent:
    from audit_model import AuditEventType  # type: ignore

    evt_type_val = getattr(row, "event_type", "USER_MESSAGE")
    try:
        evt_type = AuditEventType(evt_type_val)
    except Exception:
        # fallback: try string
        try:
            evt_type = AuditEventType[evt_type_val]
        except Exception:
            evt_type = AuditEventType.USER_MESSAGE
    ts = getattr(row, "timestamp")
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return AuditEvent(
        event_id=str(row.event_id),
        event_type=evt_type,
        timestamp=ts,
        tenant_id=str(row.tenant_id),
        user_id=getattr(row, "user_id", None),
        agent_id=getattr(row, "agent_id", None),
        session_id=getattr(row, "session_id", None),
        trace_id=getattr(row, "trace_id", None),
        request_id=getattr(row, "request_id", None),
        resource=getattr(row, "resource", None),
        action=getattr(row, "action", None),
        decision=getattr(row, "decision", None),
        policy_version=getattr(row, "policy_version", None),
        delegation_id=getattr(row, "delegation_id", None),
        credential_binding_id=getattr(row, "credential_binding_id", None),
        tool_name=getattr(row, "tool_name", None),
        parameters_hash=getattr(row, "parameters_hash", None),
        result_hash=getattr(row, "result_hash", None),
        previous_hash=getattr(row, "previous_hash", None),
        event_hash=getattr(row, "event_hash", None),
    )


class AuditLedger:
    """In-memory hash-chain ledger with optional DB persistence.

    - append: previous_hash 체이닝 + event_hash 계산
    - verify_chain: 전체 체인 무결성 검증
    - checkpoint: chain_head_hash 를 HMAC-SHA256 으로 서명하여 외부 보관
    - verify_checkpoint: checkpoint 서명 검증
    When DATABASE_URL is set, events are persisted to audit_events table.
    """

    def __init__(self, signing_key: str | None = None) -> None:
        self._head: str | None = None
        self._events: list[AuditEvent] = []
        self._signing_key = signing_key or "default-audit-signing-key"
        # hydrate from DB if available (lazy)
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        from security.models.orm import AuditEventORM  # type: ignore

                        rows = session.query(AuditEventORM).order_by(AuditEventORM.timestamp).all()  # type: ignore
                        for r in rows:
                            try:
                                evt = _orm_to_event(r)
                                self._events.append(evt)
                                self._head = evt.event_hash
                            except Exception:
                                continue
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass

    def append(self, event: AuditEvent) -> AuditEvent:
        # ensure chaining is consistent with current head (DB or memory)
        # if DB enabled, recompute head from DB if our in-memory head may be stale due to other process?
        # For simplicity, use in-memory head (already hydrated at init)
        event.previous_hash = self._head
        event.event_hash = event.compute_hash()
        self._head = event.event_hash
        self._events.append(event)
        # DB persist
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        orm = _event_to_orm(event)
                        session.add(orm)
                        session.commit()
                    except Exception as e:
                        try:
                            session.rollback()
                        except Exception:
                            pass
                        logger.debug("AuditLedger append DB persist failed: %s", e)
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass
        return event

    @property
    def head(self) -> str | None:
        if _db_enabled():
            # prefer in-memory head (already synced), but fallback to DB query if empty
            if self._head is not None:
                return self._head
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        from security.models.orm import AuditEventORM  # type: ignore

                        row = session.query(AuditEventORM).order_by(AuditEventORM.timestamp.desc()).first()  # type: ignore
                        if row is not None:
                            return getattr(row, "event_hash", None)
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass
        return self._head

    @property
    def events(self) -> list[AuditEvent]:
        if _db_enabled():
            # return DB events if we have none in memory (or always prefer DB for consistency)
            # To avoid missing events appended in same process, return in-memory if non-empty
            # but also try to ensure DB hydrate on first call
            if self._events:
                return list(self._events)
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        from security.models.orm import AuditEventORM  # type: ignore

                        rows = session.query(AuditEventORM).order_by(AuditEventORM.timestamp).all()  # type: ignore
                        evts = []
                        for r in rows:
                            try:
                                evts.append(_orm_to_event(r))
                            except Exception:
                                continue
                        if evts:
                            self._events = evts
                            self._head = evts[-1].event_hash if evts else None
                            return list(evts)
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass
        return list(self._events)

    @property
    def count(self) -> int:
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        from security.models.orm import AuditEventORM  # type: ignore

                        c = session.query(AuditEventORM).count()  # type: ignore
                        if isinstance(c, int) and c > 0:
                            return c
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass
            # fallback to memory count if DB count is 0 but memory has events (race)
            if self._events:
                return len(self._events)
        return len(self._events)

    def verify_chain(self) -> bool:
        """전체 체인 무결성 검증 — 하나라도 변조되면 False."""
        evts = self.events  # use DB-aware events
        prev: str | None = None
        for e in evts:
            if e.previous_hash != prev:
                return False
            if e.compute_hash() != e.event_hash:
                return False
            prev = e.event_hash
        return True

    # ── Checkpoint (Section 31 — 주기적 외부 서명 보관) ─────────
    def checkpoint(self, signing_key: str | None = None) -> AuditCheckpoint:
        """현재 chain head 를 HMAC-SHA256 으로 서명한 checkpoint 생성."""
        key = signing_key or self._signing_key
        head = self.head or ""
        sig = hmac.new(key.encode(), head.encode(), hashlib.sha256).hexdigest()
        return AuditCheckpoint(
            chain_head_hash=head,
            event_count=self.count,
            created_at=datetime.now(timezone.utc),
            signature=sig,
        )

    def verify_checkpoint(
        self, checkpoint: AuditCheckpoint, signing_key: str | None = None
    ) -> bool:
        """checkpoint 서명 검증."""
        key = signing_key or self._signing_key
        expected = hmac.new(
            key.encode(), checkpoint.chain_head_hash.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, checkpoint.signature):
            return False
        if checkpoint.event_count > self.count:
            return False
        if checkpoint.event_count == 0:
            return checkpoint.chain_head_hash == ""
        evts = self.events
        if checkpoint.event_count <= len(evts):
            expected_head = evts[checkpoint.event_count - 1].event_hash
            if checkpoint.chain_head_hash != expected_head and checkpoint.chain_head_hash != self.head:
                if checkpoint.chain_head_hash not in [e.event_hash for e in evts]:
                    return False
        return True

    def tamper_event(self, index: int, **kwargs) -> None:
        """테스트용 변조 — 절대 프로덕션에서 사용 금지."""
        if 0 <= index < len(self._events):
            for k, v in kwargs.items():
                setattr(self._events[index], k, v)
            # also tamper DB if enabled (so verify_chain reflects tamper)
            if _db_enabled():
                try:
                    session, engine = _db_get_session()
                    if session is not None:
                        try:
                            from security.models.orm import AuditEventORM  # type: ignore

                            row = session.query(AuditEventORM).order_by(AuditEventORM.timestamp).offset(index).first()  # type: ignore
                            if row is not None:
                                for k, v in kwargs.items():
                                    if hasattr(row, k):
                                        setattr(row, k, v)
                                session.commit()
                        except Exception:
                            try:
                                session.rollback()
                            except Exception:
                                pass
                        finally:
                            _db_close(session, engine)
                except Exception:
                    pass
