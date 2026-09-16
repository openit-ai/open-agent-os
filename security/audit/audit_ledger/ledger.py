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
import threading
from datetime import datetime, timezone
from typing import Optional

from audit_model import AuditCheckpoint, AuditEvent, AuditEventType


logger = logging.getLogger(__name__)

# Appends are serialized because the chain head is a single value: two appenders
# that read the same head both chain from it and fork the chain. The lock is
# module-level (not per instance) because callers construct a fresh AuditLedger
# per mutation. In addition, the DB path takes a transaction-scoped advisory lock
# so separate processes serialize too.
_APPEND_LOCK = threading.Lock()
ADVISORY_LOCK_KEY = 0x4F414F53  # "OAOS"

# The head is the event no other event links to. Reading it from the stored data
# (instead of a possibly stale in-memory value) is what keeps concurrent
# appenders on one chain.
_CHAIN_TIP_QUERY = (
    "SELECT a.event_hash FROM audit_events a "
    "WHERE a.event_hash IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM audit_events b WHERE b.previous_hash = a.event_hash) "
    "ORDER BY a.event_hash LIMIT 1"
)

_AUDIT_EVENT_FIELDS = (
    "event_id",
    "event_type",
    "timestamp",
    "tenant_id",
    "user_id",
    "agent_id",
    "session_id",
    "trace_id",
    "request_id",
    "resource",
    "action",
    "decision",
    "policy_version",
    "delegation_id",
    "credential_binding_id",
    "tool_name",
    "parameters_hash",
    "result_hash",
    "previous_hash",
    "event_hash",
)


def _normalize_timestamp(timestamp):
    """Use UTC-aware timestamps for hashes and database round-trips.

    SQLite's DateTime type stores a timezone-aware value as a naive value.  A
    timestamp such as ``12:00+09:00`` would therefore come back as
    ``12:00+00:00`` and change the canonical payload used by ``compute_hash``.
    Treat naive values as UTC and normalize aware values before hashing so the
    persisted representation has one stable form on every supported backend.
    """
    if timestamp is None:
        return None
    if not isinstance(timestamp, datetime):
        return timestamp
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _normalize_event_timestamp(event: AuditEvent) -> None:
    timestamp = _normalize_timestamp(getattr(event, "timestamp", None))
    if timestamp is not None:
        event.timestamp = timestamp


def _event_sort_key(event: AuditEvent) -> tuple[str, str]:
    """Stable fallback ordering for rows outside a single linked chain."""
    return (str(getattr(event, "event_id", "")), str(getattr(event, "event_hash", "")))

try:
    from sqlalchemy.exc import SQLAlchemyError
except (ImportError, ModuleNotFoundError):  # sqlalchemy is lazy/optional; best-effort fallback
    SQLAlchemyError = Exception  # type: ignore

# ── DB helpers (lazy, sync) + production fail-closed ──────────────
# Distributed state: DB is primary in production; in-memory fallback is allowed
# ONLY in non-prod (explicit test fallback). See §27/§31.
# Limitations (documented):
# - In-memory fallback is process-local — not distributed, does not survive restart, and
#   is unsuitable for HA (2+ replicas will diverge). Prod MUST set DATABASE_URL and
#   run with OAOS_ENV=production to get fail-closed guarantees. Tests rely on fallback
#   via non-prod (OAOS_ENV != production) or OAOS_ALLOW_TEST_FALLBACK=1.

def _is_prod() -> bool:
    return os.environ.get("OAOS_ENV", "").lower() in ("production", "prod")


def _allow_in_memory_fallback() -> bool:
    if _is_prod():
        return False
    fallback_flag = os.environ.get("OAOS_ALLOW_TEST_FALLBACK", "")
    if fallback_flag.lower() in ("1", "true", "yes"):
        return True
    return True if not _is_prod() else False


def _db_enabled() -> bool:
    url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    return bool(url and url.strip())


def _is_test_isolation() -> bool:
    """When running under pytest (PYTEST_CURRENT_TEST set) in non-prod, isolate DB
    to avoid preexisting DB events leaking into a fresh AuditLedger (test pollution).
    Production (OAOS_ENV=production) never isolates — fail-closed hydrate stays enabled.
    Opt-in to DB even in tests via OAOS_AUDIT_FORCE_DB=1."""
    if _is_prod():
        return False
    if os.environ.get("OAOS_AUDIT_FORCE_DB", "").lower() in ("1", "true", "yes"):
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    # also treat OAOS_ENV=test as isolation (some runners set it)
    if os.environ.get("OAOS_ENV", "").lower() in ("test", "testing"):
        return True
    return False


def _db_should_use() -> bool:
    """DB should be used only when enabled and not in test isolation."""
    return _db_enabled() and not _is_test_isolation()


def _require_db_if_prod() -> None:
    if _is_prod() and not _db_enabled():
        raise RuntimeError(
            "AuditLedger: DATABASE_URL/OAOS_DATABASE_URL required when OAOS_ENV=production "
            "(fail-closed — distributed audit state must be DB-backed)"
        )


def _normalize_sync_url(url: str) -> str:
    u = url.strip()
    # The systemd OAOS environment uses asyncpg URLs, while this synchronous
    # ledger must use the installed psycopg v3 driver (psycopg2 is not present).
    if u.startswith("postgresql+asyncpg://"):
        u = u.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    elif u.startswith("postgresql://"):
        u = u.replace("postgresql://", "postgresql+psycopg://", 1)
    if "+aiosqlite" in u:
        u = u.replace("+aiosqlite", "")
    if u.startswith("postgresql+psycopg2://"):
        u = u.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)
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
    except (ImportError, ModuleNotFoundError) as e:
        logger.debug("AuditLedger sqlalchemy import failed: %s", type(e).__name__)
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
        except (ImportError, ModuleNotFoundError, SQLAlchemyError):
            try:
                import sys
                from pathlib import Path
                sec = Path(__file__).resolve().parents[2]
                if str(sec) not in sys.path:
                    sys.path.insert(0, str(sec))
                from security.models.db import Base  # type: ignore
                from security.models.orm import AuditEventORM  # noqa: F401  # type: ignore
                Base.metadata.create_all(bind=engine)
            except (ImportError, ModuleNotFoundError, AttributeError) as e:
                logger.debug("AuditLedger metadata fallback import failed: %s", type(e).__name__)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()
        return session, engine
    except (SQLAlchemyError, ImportError, ModuleNotFoundError, OSError, RuntimeError, AttributeError, TypeError, ValueError) as e:
        logger.warning("AuditLedger DB session unavailable: %s", type(e).__name__)
        return None, None


def _db_close(session, engine) -> None:
    # Cleanup-only: close/dispose must never raise; narrowed to close-path errors.
    try:
        if session is not None:
            session.close()
    except (SQLAlchemyError, AttributeError, TypeError) as e:
        logger.debug("audit session close failed (best-effort)")
    try:
        if engine is not None:
            engine.dispose()
    except (SQLAlchemyError, AttributeError, TypeError) as e:
        logger.debug("audit engine dispose failed (best-effort)")


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
        timestamp=_normalize_timestamp(event.timestamp),
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


def _is_postgres(executor) -> bool:
    """True when the executor (Engine/Session/Connection) speaks PostgreSQL."""
    try:
        dialect = getattr(executor, "dialect", None)
        if dialect is not None:
            return getattr(dialect, "name", "") == "postgresql"
        bind = getattr(executor, "bind", None)
        if bind is not None:
            return getattr(getattr(bind, "dialect", None), "name", "") == "postgresql"
        get_bind = getattr(executor, "get_bind", None)
        if callable(get_bind):
            return getattr(getattr(get_bind(), "dialect", None), "name", "") == "postgresql"
    except (AttributeError, TypeError, RuntimeError):
        return False
    return False


def _lock_chain(executor) -> None:
    """Serialize appends for the caller's transaction.

    PostgreSQL takes a transaction-scoped advisory lock, which every process
    shares, so the read of the head and the insert of the child are atomic
    together even across processes. Other backends (sqlite in tests) already
    serialize writers.
    """
    if not _is_postgres(executor):
        return
    from sqlalchemy import text  # type: ignore

    executor.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY})


def _chain_tip(executor) -> str | None:
    """Head as stored — the event nothing links to yet."""
    from sqlalchemy import text  # type: ignore

    row = executor.execute(text(_CHAIN_TIP_QUERY)).first()
    return row[0] if row else None


def _orm_to_event(row) -> AuditEvent:
    evt_type_val = getattr(row, "event_type", "USER_MESSAGE")
    try:
        evt_type = AuditEventType(evt_type_val)
    except (ValueError, TypeError):
        # fallback: try enum name lookup, else default (read-path mapping only)
        try:
            evt_type = AuditEventType[evt_type_val]
        except (KeyError, TypeError):
            evt_type = AuditEventType.USER_MESSAGE
    ts = _normalize_timestamp(getattr(row, "timestamp"))
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


def _row_has_valid_shape(row) -> bool:
    """Return whether a DB row can represent a normal audit event.

    The ORM intentionally leaves the hash columns nullable for compatibility
    with the existing table.  Null/invalid hashes are therefore reported as
    integrity failures by the chain reconstruction instead of being filtered
    out here.
    """
    for field in ("event_id", "event_type", "timestamp", "tenant_id"):
        if getattr(row, field, None) is None:
            return False
    timestamp = getattr(row, "timestamp", None)
    if not isinstance(timestamp, datetime):
        return False
    raw_type = getattr(row, "event_type", None)
    try:
        AuditEventType(raw_type)
    except (ValueError, TypeError):
        try:
            AuditEventType[raw_type]
        except (KeyError, TypeError):
            return False
    return True


def _malformed_event_from_row(row) -> AuditEvent:
    """Build a lossless-ish model for a row that failed normal validation.

    ``AuditEvent`` is a validated model, while a malformed DB row may not be
    constructible as one.  Keep the row in the returned collection using
    Pydantic's construction escape hatch, retaining its hash/link fields so
    callers can inspect it.  The caller separately marks the snapshot
    invalid, and ``verify_chain`` consequently fails closed.
    """
    values = {field: getattr(row, field, None) for field in _AUDIT_EVENT_FIELDS}
    event_id = values.get("event_id")
    values["event_id"] = str(event_id) if event_id is not None else "<malformed-event>"
    tenant_id = values.get("tenant_id")
    values["tenant_id"] = str(tenant_id) if tenant_id is not None else ""
    values["timestamp"] = _normalize_timestamp(values.get("timestamp"))
    if not isinstance(values["timestamp"], datetime):
        values["timestamp"] = datetime.fromtimestamp(0, tz=timezone.utc)
    raw_type = values.get("event_type")
    try:
        values["event_type"] = AuditEventType(raw_type)
    except (ValueError, TypeError):
        values["event_type"] = AuditEventType.USER_MESSAGE
    construct = getattr(AuditEvent, "model_construct", None)
    if construct is not None:
        return construct(**values)
    return AuditEvent.construct(**values)  # type: ignore[attr-defined]


def _reconstruct_chain(
    events: list[AuditEvent], *, initial_integrity_error: bool = False
) -> tuple[list[AuditEvent], str | None, bool]:
    """Order DB events by their hash links and report structural failures.

    A healthy audit table has one root (``previous_hash is None``), one child
    for every non-leaf node, and one leaf.  The old hydration path sorted by
    timestamp, which is not a chain ordering and breaks as soon as clocks are
    adjusted or timestamps tie.  This routine walks the links instead.  When
    the graph is malformed it still returns every mapped row in a deterministic
    order, appending disconnected branches after the traversed roots, and marks
    the snapshot invalid for ``verify_chain``.
    """
    if not events:
        return [], None, bool(initial_integrity_error)

    integrity_error = bool(initial_integrity_error)
    by_hash: dict[str, list[AuditEvent]] = {}
    for event in events:
        event_hash = getattr(event, "event_hash", None)
        if not isinstance(event_hash, str) or not event_hash:
            integrity_error = True
            continue
        by_hash.setdefault(event_hash, []).append(event)
        try:
            if event.compute_hash() != event_hash:
                integrity_error = True
        except (AttributeError, TypeError, UnicodeError, ValueError):
            integrity_error = True

    if any(len(matches) > 1 for matches in by_hash.values()):
        integrity_error = True

    roots: list[AuditEvent] = []
    children: dict[str, list[AuditEvent]] = {}
    for event in events:
        previous_hash = getattr(event, "previous_hash", None)
        if previous_hash is None:
            roots.append(event)
            continue
        if not isinstance(previous_hash, str) or previous_hash not in by_hash:
            integrity_error = True
            continue
        children.setdefault(previous_hash, []).append(event)

    if len(roots) != 1:
        integrity_error = True
    if any(len(matches) > 1 for matches in children.values()):
        integrity_error = True

    ordered: list[AuditEvent] = []
    visited: set[int] = set()

    # A healthy table has one linear path.  Walk that path directly; malformed
    # branches/components are appended below so every row remains inspectable.
    if roots:
        current = sorted(roots, key=_event_sort_key)[0]
        while id(current) not in visited:
            visited.add(id(current))
            ordered.append(current)
            event_children = children.get(getattr(current, "event_hash", None), [])
            if len(event_children) > 1:
                integrity_error = True
                break
            if not event_children:
                break
            current = event_children[0]
        if id(current) in visited and (not ordered or ordered[-1] is not current):
            integrity_error = True

    # Missing parents, cycles, additional roots, and fork branches have not
    # been traversed.  Keep them in a deterministic tail instead of dropping
    # them from a restart snapshot.
    leftovers = sorted(
        (event for event in events if id(event) not in visited), key=_event_sort_key
    )
    if leftovers:
        integrity_error = True
        ordered.extend(leftovers)

    # A malformed graph has no singular head.  A structurally linear graph can
    # still have a useful leaf even when its payload hash is bad; verification
    # below will report that cryptographic failure separately.
    leaves = [
        event
        for event in events
        if isinstance(getattr(event, "event_hash", None), str)
        and bool(getattr(event, "event_hash", None))
        and not children.get(getattr(event, "event_hash", None), [])
    ]
    head = leaves[0].event_hash if len(leaves) == 1 else None
    return ordered, head, integrity_error


def _events_from_orm_rows(
    rows: list[object],
) -> tuple[list[AuditEvent], str | None, bool]:
    """Map all DB rows and reconstruct their linked order.

    Mapping failures are represented by a model-constructed placeholder and a
    failed integrity flag.  This keeps row counts and inspection results aligned
    with the committed table while ensuring verification never treats the bad
    row as valid.
    """
    events: list[AuditEvent] = []
    integrity_error = False
    for row in rows:
        if not _row_has_valid_shape(row):
            integrity_error = True
        try:
            event = _orm_to_event(row)
        except (ValueError, TypeError, AttributeError, KeyError, UnicodeError) as error:
            integrity_error = True
            logger.warning(
                "AuditLedger malformed audit row retained for verification: %s (%s)",
                getattr(row, "event_id", "<unknown>"),
                type(error).__name__,
            )
            event = _malformed_event_from_row(row)
        events.append(event)
    return _reconstruct_chain(events, initial_integrity_error=integrity_error)


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
        self._integrity_error = False
        self._signing_key = signing_key or "default-audit-signing-key"
        if _is_prod():
            _require_db_if_prod()
        # hydrate from DB if available (lazy) — isolated in pytest to avoid leakage
        if _db_should_use():
            self._refresh_db_state("hydration")

    def _refresh_db_state(self, operation: str) -> tuple[list[AuditEvent], str | None, int] | None:
        """Refresh the local snapshot from committed DB state.

        A ledger instance may outlive other appenders, and a caller-supplied
        transaction may later roll back.  Consequently DB-backed properties
        must not trust ``_events`` or ``_head`` as an authoritative cache.  A
        fresh session sees only committed rows and replaces the local snapshot
        on every successful read.  ``None`` means that non-production fallback
        should use the process-local snapshot because the DB is unavailable.
        """
        session, engine = _db_get_session()
        if session is None:
            error = RuntimeError(f"AuditLedger {operation} failed — audit database unavailable")
            logger.warning("AuditLedger %s unavailable", operation)
            if _is_prod():
                raise error
            return None
        try:
            from security.models.orm import AuditEventORM  # type: ignore

            rows = session.query(AuditEventORM).all()  # type: ignore
            events, head, integrity_error = _events_from_orm_rows(rows)
            self._events = events
            self._head = head
            self._integrity_error = integrity_error
            return list(events), head, len(rows)
        except (SQLAlchemyError, ImportError, ModuleNotFoundError, OSError, RuntimeError, AttributeError, TypeError, ValueError, UnicodeError) as e:
            logger.warning("AuditLedger %s failed: %s", operation, type(e).__name__)
            if _is_prod():
                raise RuntimeError(f"AuditLedger {operation} failed — audit database unavailable") from e
            return None
        finally:
            _db_close(session, engine)

    def append(self, event: AuditEvent, connection=None) -> AuditEvent:
        """Chain and persist one event.

        Pass `connection` (an open SQLAlchemy connection inside a transaction) to
        append as part of the caller's transaction, so the audit record and the
        state change it describes commit together or not at all.
        """
        if _is_prod():
            _require_db_if_prod()
        if connection is not None:
            return self._append_within(connection, event)
        with _APPEND_LOCK:
            if _db_should_use():
                return self._append_db(event)
            if _is_prod():
                raise RuntimeError("AuditLedger append failed — no DB in production (fail-closed)")
            return self._append_memory(event)

    def _append_memory(self, event: AuditEvent) -> AuditEvent:
        _normalize_event_timestamp(event)
        event.previous_hash = self._head
        event.event_hash = event.compute_hash()
        self._head = event.event_hash
        self._events.append(event)
        return event

    def _append_db(self, event: AuditEvent) -> AuditEvent:
        session, engine = _db_get_session()
        if session is None:
            if _is_prod():
                raise RuntimeError("AuditLedger append failed — audit database unavailable (fail-closed)")
            return self._append_memory(event)
        original_timestamp = event.timestamp
        original_previous_hash = event.previous_hash
        original_event_hash = event.event_hash
        error: Exception | None = None
        try:
            # The head is read inside the lock so no other appender can chain from
            # the same parent while this insert is in flight.
            _lock_chain(session)
            event.previous_hash = _chain_tip(session)
            _normalize_event_timestamp(event)
            event.event_hash = event.compute_hash()
            session.add(_event_to_orm(event))
            session.commit()
        except (SQLAlchemyError, ImportError, ModuleNotFoundError, OSError, RuntimeError, AttributeError, TypeError, ValueError) as e:
            error = e
            try:
                session.rollback()
            except SQLAlchemyError as rollback_error:
                logger.debug("AuditLedger append rollback failed: %s", type(rollback_error).__name__)
        finally:
            _db_close(session, engine)
        if error is not None:
            # A failed DB write is not a successful in-memory append.  Keep the
            # local snapshot unchanged so a later DB read cannot expose a
            # phantom event after rollback or a failed commit.
            event.timestamp = original_timestamp
            event.previous_hash = original_previous_hash
            event.event_hash = original_event_hash
            if _is_prod():
                raise RuntimeError(
                    "AuditLedger append failed — DB persist required in production but failed"
                ) from None
            logger.warning("AuditLedger append DB persist failed: %s", type(error).__name__)
            raise RuntimeError("AuditLedger append failed — DB persist failed") from None
        self._head = event.event_hash
        self._events.append(event)
        return event

    def _append_within(self, connection, event: AuditEvent) -> AuditEvent:
        """Append inside the caller's transaction; the caller commits or rolls back."""
        _lock_chain(connection)
        event.previous_hash = _chain_tip(connection)
        _normalize_event_timestamp(event)
        event.event_hash = event.compute_hash()
        orm = _event_to_orm(event)
        table = type(orm).__table__
        connection.execute(
            table.insert().values({column.name: getattr(orm, column.name) for column in table.columns})
        )
        return event

    @property
    def head(self) -> str | None:
        if _db_should_use():
            snapshot = self._refresh_db_state("head lookup")
            if snapshot is not None:
                return snapshot[1]
        return self._head

    @property
    def events(self) -> list[AuditEvent]:
        if _db_should_use():
            snapshot = self._refresh_db_state("events lookup")
            if snapshot is not None:
                return list(snapshot[0])
        return list(self._events)

    @property
    def count(self) -> int:
        if _db_should_use():
            snapshot = self._refresh_db_state("count lookup")
            if snapshot is not None:
                return snapshot[2]
        return len(self._events)

    def verify_chain(self) -> bool:
        """전체 체인 무결성 검증 — 하나라도 변조되면 False."""
        evts = self.events  # use DB-aware events
        if self._integrity_error:
            return False
        prev: str | None = None
        for e in evts:
            try:
                if e.previous_hash != prev:
                    return False
                if e.compute_hash() != e.event_hash:
                    return False
            except (AttributeError, TypeError, UnicodeError, ValueError):
                return False
            prev = e.event_hash
        return True

    # ── External anchor helpers ──────────────────────────────────
    def _external_checkpoint_path(self) -> str:
        p = os.environ.get("OAOS_AUDIT_CHECKPOINT_S3") or os.environ.get("OAOS_AUDIT_CHECKPOINT_PATH") or ""
        p = p.strip()
        if p:
            return p
        return "/var/lib/oaos/audit-checkpoint.json"

    def _write_external_checkpoint(self, cp) -> bool:
        """Best-effort write checkpoint to external storage path."""
        path = self._external_checkpoint_path()
        try:
            data = cp.model_dump(mode="json") if hasattr(cp, "model_dump") else dict(cp)
        except (AttributeError, TypeError, ValueError) as serialization_error:
            logger.warning("Audit checkpoint serialization fallback: %s", type(serialization_error).__name__)
            try:
                data = json.loads(cp.model_dump_json())  # type: ignore
            except (AttributeError, TypeError, ValueError) as json_error:
                logger.warning("Audit checkpoint JSON serialization fallback failed: %s", type(json_error).__name__)
                data = {"chain_head_hash": getattr(cp, "chain_head_hash", ""), "event_count": getattr(cp, "event_count", 0), "created_at": str(getattr(cp, "created_at", "")), "signature": getattr(cp, "signature", "")}
        if path.startswith("s3://"):
            import tempfile, subprocess, pathlib
            try:
                tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
                json.dump(data, tmp, indent=2, sort_keys=True, ensure_ascii=False)
                tmp.write("\n")
                tmp.close()
                region = os.environ.get("AWS_S3_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ap-northeast-2"
                try:
                    subprocess.run(["aws", "s3", "cp", tmp.name, path, "--region", region, "--server-side-encryption", "AES256", "--content-type", "application/json"], check=True, capture_output=True, timeout=15)
                    logger.info("Audit checkpoint anchored to S3 %s", path)
                    try:
                        os.unlink(tmp.name)
                    except OSError as cleanup_error:
                        logger.debug("S3 checkpoint temporary file cleanup failed: %s", type(cleanup_error).__name__)
                    return True
                except FileNotFoundError:
                    logger.debug("aws CLI not found, fallback to local anchor for s3 path %s", path)
                except subprocess.CalledProcessError as e:
                    logger.warning("S3 checkpoint upload failed %s: %s", path, e)
                except (OSError, ValueError, TypeError, subprocess.SubprocessError) as e:
                    logger.warning("S3 checkpoint anchor failed %s: %s", path, e)
                finally:
                    try:
                        fallback = "/tmp/oaos-audit-checkpoint.json"
                        pathlib.Path(fallback).parent.mkdir(parents=True, exist_ok=True)
                        with open(fallback, "w") as f:
                            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                            f.write("\n")
                    except (OSError, ValueError, TypeError) as fallback_error:
                        logger.debug("S3 checkpoint local fallback failed: %s", type(fallback_error).__name__)
                try:
                    os.unlink(tmp.name)
                except OSError as cleanup_error:
                    logger.debug("S3 checkpoint temporary file cleanup failed: %s", type(cleanup_error).__name__)
                return False
            except (OSError, ValueError, TypeError, RuntimeError) as e:
                logger.warning("External checkpoint S3 anchor failed: %s", e)
                return False
        try:
            from pathlib import Path
            p = Path(path)
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
            except OSError as directory_error:
                logger.debug("Audit checkpoint directory creation failed: %s", type(directory_error).__name__)
            tmp_path = str(p) + ".tmp"
            try:
                with open(tmp_path, "w") as f:
                    json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                    f.write("\n")
                os.replace(tmp_path, str(p))
            except (PermissionError, OSError, FileNotFoundError) as e:
                fallback = "/tmp/oaos-audit-checkpoint.json"
                try:
                    Path(fallback).parent.mkdir(parents=True, exist_ok=True)
                    with open(fallback, "w") as f:
                        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                        f.write("\n")
                    logger.debug("Audit checkpoint fallback to %s (original %s not writable: %s)", fallback, path, e)
                except (OSError, ValueError, TypeError) as fallback_error:
                    logger.debug("Audit checkpoint file fallback failed: %s", type(fallback_error).__name__)
                # consider fallback success as true if fallback file exists
                try:
                    if Path(fallback).exists():
                        return True
                except OSError as fallback_check_error:
                    logger.debug("Audit checkpoint fallback existence check failed: %s", type(fallback_check_error).__name__)
                return False
            logger.info("Audit checkpoint anchored to %s head=%s count=%s", path, data.get("chain_head_hash", "")[:8], data.get("event_count"))
            return True
        except (OSError, ValueError, TypeError, RuntimeError) as e:
            logger.warning("External checkpoint anchor failed for %s: %s", path, e)
            return False

    def read_external_checkpoint(self):
        """Read checkpoint from external anchor if present."""
        path = self._external_checkpoint_path()
        try:
            if path.startswith("s3://"):
                import tempfile, subprocess, json as _json
                tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
                tmp.close()
                region = os.environ.get("AWS_S3_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ap-northeast-2"
                try:
                    subprocess.run(["aws", "s3", "cp", path, tmp.name, "--region", region], check=True, capture_output=True, timeout=15)
                    with open(tmp.name) as f:
                        raw = _json.load(f)
                    os.unlink(tmp.name)
                    return AuditCheckpoint(**raw)
                except (OSError, ValueError, TypeError, subprocess.SubprocessError) as download_error:
                    logger.warning("External S3 checkpoint read failed: %s", type(download_error).__name__)
                    try:
                        os.unlink(tmp.name)
                    except OSError as cleanup_error:
                        logger.debug("External S3 checkpoint temporary cleanup failed: %s", type(cleanup_error).__name__)
                    try:
                        with open("/tmp/oaos-audit-checkpoint.json") as f:
                            raw = _json.load(f)
                        return AuditCheckpoint(**raw)
                    except (OSError, ValueError, TypeError, KeyError) as fallback_error:
                        logger.warning("External checkpoint local fallback read failed: %s", type(fallback_error).__name__)
                        return None
            else:
                from pathlib import Path
                p = Path(path)
                candidates = [p, Path("/tmp/oaos-audit-checkpoint.json")]
                for cand in candidates:
                    if cand.exists():
                        try:
                            raw = json.loads(cand.read_text())
                            return AuditCheckpoint(**raw)
                        except (OSError, ValueError, TypeError, KeyError) as parse_error:
                            logger.warning("External checkpoint candidate parse failed: %s", type(parse_error).__name__)
                return None
        except (OSError, ImportError, ModuleNotFoundError, ValueError, TypeError) as read_error:
            logger.warning("External checkpoint read failed: %s", type(read_error).__name__)
            return None

    def verify_external_checkpoint(self, signing_key: str | None = None) -> dict:
        ext = self.read_external_checkpoint()
        if ext is None:
            return {"external_exists": False, "external_verified": False, "external_checkpoint": None, "external_path": self._external_checkpoint_path()}
        sig_ok = self.verify_checkpoint(ext, signing_key=signing_key)
        head_match = (ext.chain_head_hash == (self.head or "")) if self.count > 0 else True
        return {
            "external_exists": True,
            "external_verified": bool(sig_ok),
            "external_checkpoint": ext,
            "external_path": self._external_checkpoint_path(),
            "head_match": head_match,
        }

    # ── Checkpoint (Section 31 — 주기적 외부 서명 보관) ─────────
    def checkpoint(self, signing_key: str | None = None) -> AuditCheckpoint:
        """현재 chain head 를 HMAC-SHA256 으로 서명한 checkpoint 생성. 외부 앵커에도 동기 기록."""
        key = signing_key or self._signing_key
        head = self.head or ""
        sig = hmac.new(key.encode(), head.encode(), hashlib.sha256).hexdigest()
        cp = AuditCheckpoint(
            chain_head_hash=head,
            event_count=self.count,
            created_at=datetime.now(timezone.utc),
            signature=sig,
        )
        try:
            self._write_external_checkpoint(cp)
        except (OSError, ValueError, TypeError, RuntimeError) as e:
            logger.warning("Checkpoint external anchor degraded: %s", e)
        try:
            ts = int(cp.created_at.timestamp()) if hasattr(cp.created_at, "timestamp") else int(datetime.now(timezone.utc).timestamp())
            for metric_path in ["/var/lib/node_exporter/textfile/oaos_audit.prom", "/tmp/oaos_audit.prom"]:
                try:
                    from pathlib import Path
                    Path(metric_path).parent.mkdir(parents=True, exist_ok=True)
                    lines = []
                    if Path(metric_path).exists():
                        try:
                            txt = Path(metric_path).read_text()
                            for line in txt.splitlines():
                                if "oaos_audit_last_checkpoint_timestamp" not in line:
                                    lines.append(line)
                        except (OSError, UnicodeError) as read_error:
                            logger.debug("Audit metrics read failed: %s", type(read_error).__name__)
                    lines.append("# HELP oaos_audit_last_checkpoint_timestamp Last audit checkpoint unix timestamp")
                    lines.append("# TYPE oaos_audit_last_checkpoint_timestamp gauge")
                    lines.append(f"oaos_audit_last_checkpoint_timestamp {ts}")
                    lines.append(f"oaos_audit_last_checkpoint_event_count {cp.event_count}")
                    Path(metric_path).write_text("\n".join(lines) + "\n")
                    break
                except PermissionError:
                    continue
                except (OSError, ValueError, TypeError, RuntimeError) as metric_error:
                    logger.debug("Audit metrics write failed: %s", type(metric_error).__name__)
        except (OSError, ValueError, TypeError, RuntimeError) as metrics_error:
            logger.warning("Audit metrics update degraded: %s", type(metrics_error).__name__)
        return cp

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
            if _db_should_use():
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
                        except (SQLAlchemyError, ImportError, ModuleNotFoundError, OSError, RuntimeError, AttributeError, TypeError, ValueError) as persist_error:
                            try:
                                session.rollback()
                            except SQLAlchemyError as rollback_error:
                                logger.debug("Audit tamper rollback failed: %s", type(rollback_error).__name__)
                            logger.debug("Audit tamper DB update failed: %s", type(persist_error).__name__)
                        finally:
                            _db_close(session, engine)
                except (SQLAlchemyError, ImportError, ModuleNotFoundError, OSError, RuntimeError, AttributeError, TypeError, ValueError) as lookup_error:
                    logger.debug("Audit tamper DB lookup failed: %s", type(lookup_error).__name__)
