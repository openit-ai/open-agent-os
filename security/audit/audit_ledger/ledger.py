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
        if _is_prod():
            _require_db_if_prod()
        # hydrate from DB if available (lazy) — isolated in pytest to avoid leakage
        if _db_should_use():
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
        if _is_prod():
            _require_db_if_prod()
        # ensure chaining is consistent with current head (DB or memory)
        # if DB enabled, recompute head from DB if our in-memory head may be stale due to other process?
        # For simplicity, use in-memory head (already hydrated at init)
        event.previous_hash = self._head
        event.event_hash = event.compute_hash()
        self._head = event.event_hash
        self._events.append(event)
        # DB persist — primary store in prod, in-memory only in non-prod fallback
        if _db_should_use():
            db_ok = False
            last_err: Exception | None = None
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        orm = _event_to_orm(event)
                        session.add(orm)
                        session.commit()
                        db_ok = True
                    except Exception as e:
                        last_err = e
                        try:
                            session.rollback()
                        except Exception:
                            pass
                        logger.debug("AuditLedger append DB persist failed: %s", e)
                    finally:
                        _db_close(session, engine)
            except Exception as e:
                last_err = e
            if _is_prod() and not db_ok:
                try:
                    self._events.pop()
                    self._head = self._events[-1].event_hash if self._events else None
                except Exception:
                    pass
                raise RuntimeError(f"AuditLedger append failed — DB persist required in production but failed: {last_err}")
        else:
            if _is_prod():
                raise RuntimeError("AuditLedger append failed — no DB in production (fail-closed)")
        return event

    @property
    def head(self) -> str | None:
        if _db_should_use():
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
        if _db_should_use():
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
        if _db_should_use():
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
        except Exception:
            try:
                data = json.loads(cp.model_dump_json())  # type: ignore
            except Exception:
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
                    except Exception:
                        pass
                    return True
                except FileNotFoundError:
                    logger.debug("aws CLI not found, fallback to local anchor for s3 path %s", path)
                except subprocess.CalledProcessError as e:
                    logger.warning("S3 checkpoint upload failed %s: %s", path, e)
                except Exception as e:
                    logger.warning("S3 checkpoint anchor failed %s: %s", path, e)
                finally:
                    try:
                        fallback = "/tmp/oaos-audit-checkpoint.json"
                        pathlib.Path(fallback).parent.mkdir(parents=True, exist_ok=True)
                        with open(fallback, "w") as f:
                            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                            f.write("\n")
                    except Exception:
                        pass
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass
                return False
            except Exception as e:
                logger.debug("External checkpoint S3 anchor failed: %s", e)
                return False
        try:
            from pathlib import Path
            p = Path(path)
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
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
                except Exception:
                    pass
                # consider fallback success as true if fallback file exists
                try:
                    if Path(fallback).exists():
                        return True
                except Exception:
                    pass
                return False
            logger.info("Audit checkpoint anchored to %s head=%s count=%s", path, data.get("chain_head_hash", "")[:8], data.get("event_count"))
            return True
        except Exception as e:
            logger.debug("External checkpoint anchor failed for %s: %s", path, e)
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
                except Exception:
                    try:
                        os.unlink(tmp.name)
                    except Exception:
                        pass
                    try:
                        with open("/tmp/oaos-audit-checkpoint.json") as f:
                            raw = _json.load(f)
                        return AuditCheckpoint(**raw)
                    except Exception:
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
                        except Exception:
                            continue
                return None
        except Exception:
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
        except Exception as e:
            logger.debug("Checkpoint external anchor error (ignored): %s", e)
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
                        except Exception:
                            pass
                    lines.append("# HELP oaos_audit_last_checkpoint_timestamp Last audit checkpoint unix timestamp")
                    lines.append("# TYPE oaos_audit_last_checkpoint_timestamp gauge")
                    lines.append(f"oaos_audit_last_checkpoint_timestamp {ts}")
                    lines.append(f"oaos_audit_last_checkpoint_event_count {cp.event_count}")
                    Path(metric_path).write_text("\n".join(lines) + "\n")
                    break
                except PermissionError:
                    continue
                except Exception:
                    continue
        except Exception:
            pass
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
                        except Exception:
                            try:
                                session.rollback()
                            except Exception:
                                pass
                        finally:
                            _db_close(session, engine)
                except Exception:
                    pass
