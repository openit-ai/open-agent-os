"""File-backed audit restart and committed-state regressions."""

from datetime import datetime, timedelta, timezone

import pytest
from audit_model import AuditEvent, AuditEventType
from sqlalchemy import create_engine, text

from security.audit.audit_ledger.ledger import AuditLedger


def _event(number: int, timestamp: datetime) -> AuditEvent:
    return AuditEvent(
        event_id=f"restart-event-{number}",
        event_type=AuditEventType.POLICY_DECISION,
        timestamp=timestamp,
        tenant_id="restart-test",
        user_id="tester@example.com",
        resource=f"resource/{number}",
        action="TEST",
        decision="ALLOW",
    )


@pytest.fixture()
def sqlite_audit_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'audit.sqlite'}"
    monkeypatch.setenv("OAOS_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("OAOS_DATABASE_URL", url)
    monkeypatch.setenv("OAOS_AUDIT_FORCE_DB", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    return url


def _insert_event(connection, event: AuditEvent) -> None:
    from security.audit.audit_ledger import ledger as ledger_mod

    orm = ledger_mod._event_to_orm(event)
    table = type(orm).__table__
    connection.execute(
        table.insert().values({column.name: getattr(orm, column.name) for column in table.columns})
    )


def test_restart_rebuilds_order_from_previous_hash(sqlite_audit_db):
    """Clock ordering, including tied timestamps, does not define the chain."""
    first = AuditLedger(signing_key="restart-key")
    first.append(_event(1, datetime(2026, 1, 3, 12, tzinfo=timezone(timedelta(hours=9)))))
    first.append(_event(2, datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=9)))))
    first.append(_event(3, datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=9)))))

    restarted = AuditLedger(signing_key="restart-key")

    assert [event.event_id for event in restarted.events] == [
        "restart-event-1",
        "restart-event-2",
        "restart-event-3",
    ]
    assert restarted.count == 3
    assert restarted.head == restarted.events[-1].event_hash
    assert restarted.verify_chain() is True
    assert all(event.timestamp.tzinfo == timezone.utc for event in restarted.events)


def test_existing_instance_reads_other_append_and_caller_rollback(sqlite_audit_db):
    observer = AuditLedger(signing_key="restart-key")
    writer = AuditLedger(signing_key="restart-key")
    committed = writer.append(_event(1, datetime(2026, 1, 1, tzinfo=timezone.utc)))

    assert observer.count == 1
    assert [event.event_id for event in observer.events] == ["restart-event-1"]
    assert observer.head == committed.event_hash

    engine = create_engine(sqlite_audit_db)
    rolled_back = _event(2, datetime(2026, 1, 2, tzinfo=timezone.utc))
    try:
        with pytest.raises(RuntimeError, match="caller aborts"), engine.begin() as connection:
            observer.append(rolled_back, connection=connection)
            assert connection.execute(text("SELECT count(*) FROM audit_events")).scalar() == 2

            # The caller's uncommitted row must not enter a DB-backed read or
            # local cache before the transaction outcome is known.
            assert observer.count == 1
            assert [event.event_id for event in observer.events] == ["restart-event-1"]
            assert observer.head == committed.event_hash
            raise RuntimeError("caller aborts")

        assert observer.count == 1
        assert [event.event_id for event in observer.events] == ["restart-event-1"]
        assert observer.head == committed.event_hash
        assert observer.verify_chain() is True

        with engine.begin() as connection:
            persisted = observer.append(
                _event(3, datetime(2026, 1, 3, tzinfo=timezone.utc)),
                connection=connection,
            )
        assert observer.count == 2
        assert observer.head == persisted.event_hash
        assert observer.verify_chain() is True
    finally:
        engine.dispose()


def test_forked_rows_are_retained_and_fail_verification(sqlite_audit_db):
    ledger = AuditLedger(signing_key="restart-key")
    root = ledger.append(_event(1, datetime(2026, 1, 1, tzinfo=timezone.utc)))
    ledger.append(_event(2, datetime(2026, 1, 2, tzinfo=timezone.utc)))

    branch = _event(3, datetime(2026, 1, 3, tzinfo=timezone.utc))
    branch.previous_hash = root.event_hash
    branch.event_hash = branch.compute_hash()
    engine = create_engine(sqlite_audit_db)
    try:
        with engine.begin() as connection:
            _insert_event(connection, branch)

        restarted = AuditLedger(signing_key="restart-key")
        assert restarted.count == 3
        assert {event.event_id for event in restarted.events} == {
            "restart-event-1",
            "restart-event-2",
            "restart-event-3",
        }
        assert restarted.verify_chain() is False
    finally:
        engine.dispose()


def test_failed_db_insert_does_not_enter_local_cache(sqlite_audit_db):
    ledger = AuditLedger(signing_key="restart-key")
    committed = ledger.append(_event(1, datetime(2026, 1, 1, tzinfo=timezone.utc)))

    # Reusing the primary key makes the DB insert fail after the event has been
    # hashed.  The failed row must not become a phantom local event.
    duplicate = _event(1, datetime(2026, 1, 2, tzinfo=timezone.utc))
    with pytest.raises(RuntimeError, match="DB persist failed"):
        ledger.append(duplicate)

    assert ledger.count == 1
    assert [event.event_id for event in ledger.events] == [committed.event_id]
    assert ledger.head == committed.event_hash
    assert ledger.verify_chain() is True
