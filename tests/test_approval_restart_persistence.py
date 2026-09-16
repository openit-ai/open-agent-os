"""Durable approval lifecycle checks for the sync SQLAlchemy store."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from approval_workflow import workflow
from approval_workflow.workflow import ApprovalDecision, ApprovalStore


pytestmark = pytest.mark.subsystem


@pytest.fixture
def approval_db(tmp_path, monkeypatch):
    db_file = tmp_path / "approval.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.delenv("OAOS_DATABASE_URL", raising=False)
    monkeypatch.setenv("OAOS_APPROVAL_FORCE_DB", "1")
    monkeypatch.setenv("OAOS_ENV", "development")
    return db_file


def _request(store: ApprovalStore):
    return store.create(
        user_id="employee:kim",
        agent_id="agent:assistant:kim",
        action="MERGE",
        resource="github/org/repo/pr/1",
        risk="HIGH",
    )


def test_pending_request_recreated_from_file_db(approval_db):
    first = ApprovalStore(signing_key="restart-signing-key")
    created = _request(first)

    restarted = ApprovalStore(signing_key="restart-signing-key")
    loaded = restarted.get(created.approval_id)

    assert loaded is not None
    assert loaded.decision is ApprovalDecision.PENDING
    assert loaded.expires_at.tzinfo is not None
    assert loaded.expires_at.utcoffset() == timedelta(0)
    assert restarted.verify(loaded)


@pytest.mark.parametrize(
    ("decision", "group_id"),
    [
        (ApprovalDecision.DENIED, None),
        (ApprovalDecision.APPROVED_ONCE, None),
        (ApprovalDecision.APPROVED_USER_ALWAYS, None),
        (ApprovalDecision.APPROVED_GROUP_ALWAYS, "group:dev"),
    ],
)
def test_every_decision_roundtrips_after_restart(approval_db, decision, group_id):
    first = ApprovalStore(signing_key="restart-signing-key")
    created = _request(first)
    first.decide(
        created.approval_id,
        decision,
        decided_by="manager:lee",
        group_id=group_id,
    )

    restarted = ApprovalStore(signing_key="restart-signing-key")
    loaded = restarted.get(created.approval_id)

    assert loaded is not None
    assert loaded.decision is decision
    assert loaded.decided_by == "manager:lee"
    assert loaded.decided_at is not None
    assert loaded.decided_at.tzinfo is not None
    assert loaded.decided_at.utcoffset() == timedelta(0)
    assert restarted.verify(loaded)
    if decision is ApprovalDecision.APPROVED_USER_ALWAYS:
        assert restarted.has_user_grant("employee:kim", "MERGE", "github/org/repo/pr/1")
    elif decision is ApprovalDecision.APPROVED_GROUP_ALWAYS:
        assert restarted.has_group_grant("group:dev", "MERGE", "github/org/repo/pr/1")


def test_db_missing_row_does_not_resurrect_cached_request(approval_db):
    store = ApprovalStore(signing_key="restart-signing-key")
    created = _request(store)

    session, engine = workflow._db_get_session()
    assert session is not None
    try:
        from security.models.orm import ApprovalRequestORM

        session.query(ApprovalRequestORM).filter(
            ApprovalRequestORM.approval_id == created.approval_id
        ).delete(synchronize_session=False)
        session.commit()
    finally:
        workflow._db_close(session, engine)

    assert store.get(created.approval_id) is None
    assert created.approval_id not in store._requests


def test_grants_refresh_from_db_and_survive_failed_refresh(approval_db, monkeypatch):
    store = ApprovalStore(signing_key="restart-signing-key")
    user_req = _request(store)
    store.decide(user_req.approval_id, ApprovalDecision.APPROVED_USER_ALWAYS, "manager:lee")
    group_req = _request(store)
    store.decide(
        group_req.approval_id,
        ApprovalDecision.APPROVED_GROUP_ALWAYS,
        "manager:lee",
        group_id="group:dev",
    )

    session, engine = workflow._db_get_session()
    assert session is not None
    try:
        from security.models.orm import ApprovalRequestORM

        session.query(ApprovalRequestORM).filter(
            ApprovalRequestORM.approval_id == user_req.approval_id
        ).update({"decision": ApprovalDecision.DENIED.value}, synchronize_session=False)
        session.query(ApprovalRequestORM).filter(
            ApprovalRequestORM.approval_id == group_req.approval_id
        ).update({"decision": ApprovalDecision.DENIED.value}, synchronize_session=False)
        session.commit()
    finally:
        workflow._db_close(session, engine)

    # A successful read replaces the local grant cache with DB authority.
    assert not store.has_user_grant("employee:kim", "MERGE", "github/org/repo/pr/1")
    assert not store.has_group_grant("group:dev", "MERGE", "github/org/repo/pr/1")

    # A failed read leaves the last known cache available in non-production.
    store._user_grants.add(("employee:kim", "MERGE", "github/org/repo/pr/1"))
    store._group_grants.add(("group:dev", "MERGE", "github/org/repo/pr/1"))
    monkeypatch.setattr(workflow, "_db_get_session", lambda: (None, None))
    assert store.has_user_grant("employee:kim", "MERGE", "github/org/repo/pr/1")
    assert store.has_group_grant("group:dev", "MERGE", "github/org/repo/pr/1")


def test_nonce_collision_is_replay_and_leaves_pending_row(approval_db):
    store = ApprovalStore(signing_key="restart-signing-key")
    created = _request(store)

    session, engine = workflow._db_get_session()
    assert session is not None
    try:
        from security.models.orm import ApprovalNonceORM

        now = datetime.now(timezone.utc)
        session.add(
            ApprovalNonceORM(
                nonce=created.nonce,
                created_at=now,
                expires_at=now + timedelta(seconds=workflow.NONCE_TTL_SECONDS),
            )
        )
        session.commit()
    finally:
        workflow._db_close(session, engine)

    with pytest.raises(ValueError, match="nonce replay"):
        store.decide(created.approval_id, ApprovalDecision.APPROVED_ONCE, "manager:lee")

    assert store.get(created.approval_id).decision is ApprovalDecision.PENDING


def test_failed_decision_transaction_rolls_back_and_retry_succeeds(approval_db, monkeypatch):
    store = ApprovalStore(signing_key="restart-signing-key")
    created = _request(store)
    original_get_session = workflow._db_get_session
    fail_commit = {"enabled": False}

    def injected_get_session():
        session, engine = original_get_session()
        if session is not None and fail_commit["enabled"]:
            def commit_once():
                fail_commit["enabled"] = False
                raise RuntimeError("injected commit failure")

            session.commit = commit_once
        return session, engine

    monkeypatch.setattr(workflow, "_db_get_session", injected_get_session)
    fail_commit["enabled"] = True
    with pytest.raises(RuntimeError, match="decision persistence"):
        store.decide(created.approval_id, ApprovalDecision.APPROVED_ONCE, "manager:lee")

    assert store.get(created.approval_id).decision is ApprovalDecision.PENDING
    session, engine = original_get_session()
    assert session is not None
    try:
        from security.models.orm import ApprovalNonceORM, ApprovalRequestORM

        assert session.query(ApprovalNonceORM).filter(
            ApprovalNonceORM.nonce == created.nonce
        ).first() is None
        row = session.query(ApprovalRequestORM).filter(
            ApprovalRequestORM.approval_id == created.approval_id
        ).first()
        assert row is not None
        assert row.decision == ApprovalDecision.PENDING.value
    finally:
        workflow._db_close(session, engine)

    retried = store.decide(created.approval_id, ApprovalDecision.APPROVED_ONCE, "manager:lee")
    assert retried.decision is ApprovalDecision.APPROVED_ONCE


def test_concurrent_decisions_only_one_wins(approval_db):
    creator = ApprovalStore(signing_key="restart-signing-key")
    created = _request(creator)
    barrier = threading.Barrier(2)
    results = []

    def decide(decision):
        store = ApprovalStore(signing_key="restart-signing-key")
        barrier.wait()
        try:
            results.append(store.decide(created.approval_id, decision, "manager:lee"))
        except Exception as exc:  # noqa: BLE001 - one loser is expected under CAS/unique claim
            results.append(exc)

    threads = [
        threading.Thread(target=decide, args=(ApprovalDecision.APPROVED_ONCE,)),
        threading.Thread(target=decide, args=(ApprovalDecision.DENIED,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 2
    assert sum(isinstance(result, workflow.ApprovalRequest) for result in results) == 1
    assert sum(isinstance(result, Exception) for result in results) == 1
    final = ApprovalStore(signing_key="restart-signing-key").get(created.approval_id)
    assert final is not None
    assert final.decision in (ApprovalDecision.APPROVED_ONCE, ApprovalDecision.DENIED)
