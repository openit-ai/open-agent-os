"""Existing migrations alone support approval/audit restart persistence."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from audit_model import AuditEvent, AuditEventType
from sqlalchemy import create_engine

from security.approval.approval_workflow.workflow import ApprovalDecision, ApprovalStore
from security.audit.audit_ledger.ledger import AuditLedger
from security.models.db import Base

pytestmark = pytest.mark.subsystem
ROOT = Path(__file__).resolve().parents[1]


def test_existing_migrations_support_restart_without_runtime_ddl(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("OAOS_DATABASE_URL", url)
    monkeypatch.setenv("OAOS_ENV", "production")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            for revision in ("001_initial_persistence", "008_approval_nonces"):
                spec = importlib.util.spec_from_file_location(
                    revision, ROOT / "alembic" / "versions" / f"{revision}.py"
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                monkeypatch.setattr(module, "op", operations)
                module.upgrade()
    finally:
        engine.dispose()

    # Disable the runtime convenience DDL: persistence must use only the schema
    # installed by the existing migrations, without silently repairing it.
    monkeypatch.setattr(Base.metadata, "create_all", lambda *args, **kwargs: None)
    store = ApprovalStore("migration-test-key")
    request = store.create("user", "agent", "READ", "documents/*")
    store.decide(request.approval_id, ApprovalDecision.APPROVED_GROUP_ALWAYS, "reviewer", "team")
    ledger = AuditLedger("migration-test-key")
    event = ledger.append(AuditEvent(
        event_id="migration-event",
        event_type=AuditEventType.APPROVAL_DECISION,
        timestamp=datetime.now(timezone.utc),
        tenant_id="test",
        decision="APPROVED_GROUP_ALWAYS",
    ))

    restored = ApprovalStore("migration-test-key")
    assert restored.get(request.approval_id).decision == ApprovalDecision.APPROVED_GROUP_ALWAYS
    assert restored.has_group_grant("team", "READ", "documents/report")
    restarted = AuditLedger("migration-test-key")
    assert restarted.head == event.event_hash
    assert restarted.count == 1
    assert restarted.verify_chain()
