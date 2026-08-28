"""Minimal guard tests — revoke/classification (in-memory only, no DB).

Covers new §29 guard: CONFIDENTIAL/SECRET/PII + permanent rejected,
and revoke cascade invariants. Runs via MemoryStore in-memory fallback
so no DATABASE_URL / postgres dependency.
"""
from __future__ import annotations

import pytest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for p in [ROOT / "security/memory-governance", ROOT / "memory_service"]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from governance.governance import MemoryScope, MemoryStore  # type: ignore


class TestClassificationRetentionGuard:
    def test_confidential_permanent_rejected(self):
        store = MemoryStore()
        with pytest.raises(ValueError, match="incompatible with permanent"):
            store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="secret plan", classification="CONFIDENTIAL", retention_policy="permanent")

    def test_secret_and_pii_permanent_rejected(self):
        store = MemoryStore()
        for cls in ("SECRET", "PII"):
            with pytest.raises(ValueError, match="incompatible with permanent"):
                store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="x", classification=cls, retention_policy="permanent")

    def test_internal_and_public_permanent_allowed(self):
        store = MemoryStore()
        for cls in ("INTERNAL", "PUBLIC"):
            rec = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="ok", classification=cls, retention_policy="permanent")
            assert rec.retention_policy == "permanent"
            assert rec.classification == cls

    def test_confidential_standard_allowed(self):
        store = MemoryStore()
        rec = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="ok", classification="CONFIDENTIAL", retention_policy="standard")
        assert rec.classification == "CONFIDENTIAL"
        rec2 = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="ok", classification="CONFIDENTIAL", retention_policy="long_term")
        assert rec2.retention_policy == "long_term"

    def test_guard_via_memory_service_validation(self):
        """Memory service uses same governance validation — 422 equivalent here is ValueError."""
        store = MemoryStore()
        # simulate service write path: ensure guard fires before persistence
        with pytest.raises(ValueError):
            store.write(owner="employee:kim", scope="personal", content="confidential permanent via str scope", classification="CONFIDENTIAL", retention_policy="permanent")


class TestRevokeCascadeGuard:
    def test_revoke_removes_only_target_delegation(self):
        store = MemoryStore()
        r1 = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="a", source_delegation_id="dlg_1")
        r2 = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="b", source_delegation_id="dlg_1")
        r3 = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="c", source_delegation_id="dlg_2")
        assert store.invalidate_by_delegation("dlg_1") == 2
        assert store.read(r1.id, requester="employee:kim") is None
        assert store.read(r2.id, requester="employee:kim") is None
        assert store.read(r3.id, requester="employee:kim") is not None
        # idempotent second call
        assert store.invalidate_by_delegation("dlg_1") == 0

    def test_revoke_by_resource(self):
        store = MemoryStore()
        r = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="x", source_resource_id="gmail/user/kim/messages/1")
        assert store.invalidate_by_resource("gmail/user/kim/messages/1") == 1
        assert store.get(r.id).invalidated is True
        assert store.read(r.id, requester="employee:kim") is None
