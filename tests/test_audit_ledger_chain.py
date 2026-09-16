"""Hash-chain integrity under concurrency.

Two defects are covered:

1. `AuditLedger.append` chained from a per-process in-memory head. An instance
   that was constructed before another appender wrote had a stale head, so both
   children pointed at the same parent and the chain forked.
2. The chain tip is the event nothing links to, so it must be read from stored
   state rather than assumed from a process-local value.

Path note: `tests/conftest.py` already puts the repo root and the shared packages
on `sys.path`, which is all this module needs. Do NOT add `security/` here — that
makes `import auth` resolve to `security/auth.py` (which has none of the admin auth
symbols) for every later test in the run.
"""
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from audit_model import AuditEvent, AuditEventType

from security.audit.audit_ledger import ledger as ledger_mod

ROOT = Path(__file__).resolve().parents[1]


def _event(n: int, action: str = "TEST") -> AuditEvent:
    return AuditEvent(
        event_id=f"evt_test_{n:04d}",
        event_type=AuditEventType.POLICY_DECISION,
        timestamp=datetime.now(UTC),
        tenant_id="default",
        user_id="tester@openit.co.kr",
        resource=f"resource/{n}",
        action=action,
        decision="ALLOW",
    )


@pytest.fixture()
def sqlite_ledger(tmp_path, monkeypatch):
    """Point the ledger at a throwaway sqlite database (non-prod)."""
    db = tmp_path / "audit.sqlite"
    monkeypatch.setenv("OAOS_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("OAOS_DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("OAOS_AUDIT_FORCE_DB", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    import security.audit.audit_ledger.ledger as mod

    return mod


def test_stale_instance_chains_onto_the_stored_tip(sqlite_ledger):
    """An instance built before another append must not fork the chain."""
    mod = sqlite_ledger
    first = mod.AuditLedger(signing_key="k")  # hydrates an empty chain
    second = mod.AuditLedger(signing_key="k")  # same starting point

    first.append(_event(1))
    second.append(_event(2))  # its in-memory head is stale — must read the tip

    fresh = mod.AuditLedger(signing_key="k")
    assert len(fresh.events) == 2
    assert fresh.verify_chain() is True
    assert fresh.events[1].previous_hash == fresh.events[0].event_hash


def test_concurrent_appends_produce_one_chain(sqlite_ledger):
    """Parallel appenders, each with its own instance, stay on a single chain."""
    mod = sqlite_ledger
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        try:
            mod.AuditLedger(signing_key="k").append(_event(n))
        except Exception as exc:  # noqa: BLE001 - reported through `errors`
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    fresh = mod.AuditLedger(signing_key="k")
    assert len(fresh.events) == 16
    assert fresh.verify_chain() is True


def test_append_in_caller_transaction_rolls_back_with_it(sqlite_ledger):
    """An append made inside a caller's transaction disappears if it aborts."""
    from sqlalchemy import create_engine, text

    mod = sqlite_ledger
    mod.AuditLedger(signing_key="k").append(_event(1))  # a committed event

    engine = create_engine(mod._db_sync_url())
    ledger = mod.AuditLedger(signing_key="k")
    with pytest.raises(RuntimeError, match="caller aborts"), engine.begin() as connection:
        ledger.append(_event(2), connection=connection)
        assert connection.execute(text("SELECT count(*) FROM audit_events")).scalar() == 2
        raise RuntimeError("caller aborts")

    fresh = mod.AuditLedger(signing_key="k")
    assert len(fresh.events) == 1
    assert fresh.verify_chain() is True


def test_append_in_caller_transaction_commits_with_it(sqlite_ledger):
    from sqlalchemy import create_engine

    mod = sqlite_ledger
    mod.AuditLedger(signing_key="k").append(_event(1))

    engine = create_engine(mod._db_sync_url())
    ledger = mod.AuditLedger(signing_key="k")
    with engine.begin() as connection:
        ledger.append(_event(2), connection=connection)

    fresh = mod.AuditLedger(signing_key="k")
    assert len(fresh.events) == 2
    assert fresh.verify_chain() is True
    assert fresh.events[1].previous_hash == fresh.events[0].event_hash


def test_chain_tip_follows_the_leaf_not_the_newest_row(sqlite_ledger):
    """The head is the event nothing links to."""
    mod = sqlite_ledger
    ledger = mod.AuditLedger(signing_key="k")
    parent = ledger.append(_event(1))
    child = ledger.append(_event(2))

    # _chain_tip is the helper that reads the stored tip; looked up here so this
    # module still collects against the previous implementation and fails on the
    # behaviour rather than on the import.
    chain_tip = ledger_mod._chain_tip
    session, engine = ledger_mod._db_get_session()
    try:
        assert chain_tip(session) == child.event_hash
        assert chain_tip(session) != parent.event_hash
    finally:
        ledger_mod._db_close(session, engine)
