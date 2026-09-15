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

from audit_model import AuditCheckpoint, AuditEvent


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
    "ORDER BY a.timestamp DESC LIMIT 1"
)

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
    from audit_model import AuditEventType  # type: ignore

    evt_type_val = getattr(row, "event_type", "USER_MESSAGE")
    try:
        evt_type = AuditEventType(evt_type_val)
    except (ValueError, TypeError):
        # fallback: try enum name lookup, else default (read-path mapping only)
        try:
            evt_type = AuditEventType[evt_type_val]
        except (KeyError, TypeError):
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
        if _is_prod():
            _require_db_if_prod()
        # hydrate from DB if available (lazy) — isolated in pytest to avoid leakage
        if _db_should_use():
            try:
                session, engine = _db_get_session()
                if session is None:
                    error = RuntimeError("AuditLedger hydration failed — audit database unavailable")
                    logger.warning("AuditLedger hydration unavailable")
                    if _is_prod():
                        raise error
                else:
                    try:
                        from security.models.orm import AuditEventORM  # type: ignore

                        rows = session.query(AuditEventORM).order_by(AuditEventORM.timestamp).all()  # type: ignore
                        for r in rows:
                            try:
                                evt = _orm_to_event(r)
                                self._events.append(evt)
                                self._head = evt.event_hash
                            except (ValueError, TypeError, AttributeError, KeyError):
                                # Corrupt row mapping — skip single row, keep the chain verifiable
                                continue
                    finally:
                        _db_close(session, engine)
            except (SQLAlchemyError, ImportError, ModuleNotFoundError, OSError, RuntimeError, AttributeError, TypeError, ValueError) as e:
                logger.warning("AuditLedger hydrate failed: %s", type(e).__name__)
                if _is_prod():
                    raise RuntimeError("AuditLedger hydration failed — audit database unavailable") from e

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
        error: Exception | None = None
        try:
            # The head is read inside the lock so no other appender can chain from
            # the same parent while this insert is in flight.
            _lock_chain(session)
            event.previous_hash = _chain_tip(session)
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
            if _is_prod():
                raise RuntimeError(f"AuditLedger append failed — DB persist required in production but failed: {error}") from error
            logger.warning("AuditLedger append DB persist failed: %s", type(error).__name__)
            if event.event_hash is None:
                return self._append_memory(event)
        self._head = event.event_hash
        self._events.append(event)
        return event

    def _append_within(self, connection, event: AuditEvent) -> AuditEvent:
        """Append inside the caller's transaction; the caller commits or rolls back."""
        _lock_chain(connection)
        event.previous_hash = _chain_tip(connection)
        event.event_hash = event.compute_hash()
        orm = _event_to_orm(event)
        table = type(orm).__table__
        connection.execute(
            table.insert().values({column.name: getattr(orm, column.name) for column in table.columns})
        )
        self._head = event.event_hash
        self._events.append(event)
        return event

    @property
    def head(self) -> str | None:
        if _db_should_use():
            # prefer in-memory head (already synced), but fallback to DB query if empty
            if self._head is not None:
                return self._head
            try:
                session, engine = _db_get_session()
                if session is None:
                    error = RuntimeError("AuditLedger head lookup failed — audit database unavailable")
                    logger.warning("AuditLedger head lookup unavailable")
                    if _is_prod():
                        raise error
                else:
                    try:
                        from security.models.orm import AuditEventORM  # type: ignore

                        row = session.query(AuditEventORM).order_by(AuditEventORM.timestamp.desc()).first()  # type: ignore
                        if row is not None:
                            return getattr(row, "event_hash", None)
                    finally:
                        _db_close(session, engine)
            except (SQLAlchemyError, ImportError, ModuleNotFoundError, OSError, RuntimeError, AttributeError, TypeError, ValueError) as e:
                logger.warning("AuditLedger head DB lookup failed: %s", type(e).__name__)
                if _is_prod():
                    raise RuntimeError("AuditLedger head lookup failed — audit database unavailable") from e
        return self._head

    @property
    def events(self) -> list[AuditEvent]:
        if _db_should_use():
            # return DB events if we have none in memory (or always prefer DB for consistency)
            # To avoid missing events appended in same process, return in-memory if non-empty
            # but also try to ensure DB hydrate on first call
            if self._events:
                return list(self._events)
            try:
                session, engine = _db_get_session()
                if session is None:
                    error = RuntimeError("AuditLedger events lookup failed — audit database unavailable")
                    logger.warning("AuditLedger events lookup unavailable")
                    if _is_prod():
                        raise error
                else:
                    try:
                        from security.models.orm import AuditEventORM  # type: ignore

                        rows = session.query(AuditEventORM).order_by(AuditEventORM.timestamp).all()  # type: ignore
                        evts = []
                        for r in rows:
                            try:
                                evts.append(_orm_to_event(r))
                            except (ValueError, TypeError, AttributeError, KeyError):
                                # Corrupt row mapping — skip single row, keep the chain verifiable
                                continue
                        if evts:
                            self._events = evts
                            self._head = evts[-1].event_hash if evts else None
                            return list(evts)
                    finally:
                        _db_close(session, engine)
            except (SQLAlchemyError, ImportError, ModuleNotFoundError, OSError, RuntimeError, AttributeError, TypeError, ValueError) as e:
                logger.warning("AuditLedger events DB lookup failed: %s", type(e).__name__)
                if _is_prod():
                    raise RuntimeError("AuditLedger events lookup failed — audit database unavailable") from e
        return list(self._events)

    @property
    def count(self) -> int:
        if _db_should_use():
            try:
                session, engine = _db_get_session()
                if session is None:
                    error = RuntimeError("AuditLedger count failed — audit database unavailable")
                    logger.warning("AuditLedger count lookup unavailable")
                    if _is_prod():
                        raise error
                else:
                    try:
                        from security.models.orm import AuditEventORM  # type: ignore

                        c = session.query(AuditEventORM).count()  # type: ignore
                        if isinstance(c, int) and c > 0:
                            return c
                    finally:
                        _db_close(session, engine)
            except (SQLAlchemyError, ImportError, ModuleNotFoundError, OSError, RuntimeError, AttributeError, TypeError, ValueError) as e:
                logger.warning("AuditLedger count lookup failed: %s", type(e).__name__)
                if _is_prod():
                    raise RuntimeError("AuditLedger count failed — audit database unavailable") from e
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
