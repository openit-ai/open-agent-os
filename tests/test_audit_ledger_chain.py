"""Hash-chain integrity under concurrency, and atomic audit for policy mutations.

Two defects are covered:

1. `AuditLedger.append` chained from a per-process in-memory head. An instance
   that was constructed before another appender wrote had a stale head, so both
   children pointed at the same parent and the chain forked. Production ran with
   exactly that shape: a long-lived ledger instance next to per-mutation ones.
2. `policy.publish` committed the state change and only then appended its audit
   record, so a failing ledger left a published bundle with no audit trail (the
   publish request returned 500 while the new version stayed active).
"""
import importlib
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in [
    ROOT,
    ROOT / "security",
    ROOT / "security" / "audit",
    ROOT / "packages" / "audit-model",
    ROOT / "packages" / "policy-model",
    ROOT / "admin-console",
]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from audit_model import AuditEvent, AuditEventType


def _ledger_module():
    import security.audit.audit_ledger.ledger as mod

    return importlib.reload(mod)


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
    """A ledger module pointed at a throwaway sqlite database (non-prod)."""
    db = tmp_path / "audit.sqlite"
    monkeypatch.setenv("OAOS_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("OAOS_DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("OAOS_AUDIT_FORCE_DB", "1")
    mod = _ledger_module()
    yield mod


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
    from sqlalchemy import create_engine

    mod = sqlite_ledger
    mod.AuditLedger(signing_key="k").append(_event(1))  # a committed event

    engine = create_engine(f"sqlite:///{Path(mod._db_sync_url().replace('sqlite:///', ''))}")
    ledger = mod.AuditLedger(signing_key="k")
    with pytest.raises(RuntimeError, match="caller aborts"), engine.begin() as connection:
        ledger.append(_event(2), connection=connection)
        assert connection.execute(
            __import__("sqlalchemy").text("SELECT count(*) FROM audit_events")
        ).scalar() == 2  # visible inside the transaction
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
    """The head is the event nothing links to, so an orphan cannot become the tip."""
    mod = sqlite_ledger
    ledger = mod.AuditLedger(signing_key="k")
    parent = ledger.append(_event(1))
    child = ledger.append(_event(2))

    session, engine = mod._db_get_session()
    try:
        assert mod._chain_tip(session) == child.event_hash
        assert mod._chain_tip(session) != parent.event_hash
    finally:
        mod._db_close(session, engine)
