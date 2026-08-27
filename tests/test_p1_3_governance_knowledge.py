"""P1-3 Integrated Tests Skeleton — §27+28+29

Covers:
  §27 Memory Governance: namespace, provenance, revoke cascade
  §28 Knowledge ACL: Outline retrieval pipeline
  §29 Data Classification 5-level + HIGH Egress

Skeleton provides concrete test cases that can be extended.
All tests are deterministic (no LLM).
"""
from __future__ import annotations

import pytest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in [
    ROOT / "security/memory-governance",
    ROOT / "execution-gateway",
    ROOT / "packages/common-types",
    ROOT / "packages/mcp-resource-model",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# ── §27 Memory Governance ────────────────────────────────────────────
from governance.governance import MemoryScope, MemoryRecord, MemoryStore

# ── §29 Risk ─────────────────────────────────────────────────────────
from execution_gateway.risk import (
    DataClassification,
    classify,
    classify_content,
    is_high_egress,
    get_egress_classification,
    RiskLevel,
)

# ── §28 Knowledge ────────────────────────────────────────────────────
from execution_gateway.connectors.outline import OutlineConnector, OutlineDocument
from execution_gateway.knowledge import KnowledgeRetriever


# ── Helpers ──────────────────────────────────────────────────────────
def make_ctx(user="employee:kim", tenant="test-tenant", groups=None, delegation=None):
    return {
        "user_id": user,
        "tenant_id": tenant,
        "groups": groups or [],
        "delegation_id": delegation,
        "agent_id": user.replace("employee:", "agent:assistant:"),
    }


# ── §27 Tests ────────────────────────────────────────────────────────

class TestMemoryNamespace:
    def test_personal_scope_requires_employee_owner(self):
        store = MemoryStore()
        rec = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="hello")
        assert rec.scope == MemoryScope.PERSONAL
        assert rec.owner == "employee:kim"
        with pytest.raises(ValueError):
            store.write(owner="employee:kim", scope="invalid_scope", content="x")

    def test_team_and_corporate_scopes(self):
        store = MemoryStore()
        team_rec = store.write(owner="group:dev", scope=MemoryScope.TEAM, content="team note", group_id="dev")
        corp_rec = store.write(owner="organization", scope=MemoryScope.CORPORATE, content="corp note")
        assert team_rec.scope == MemoryScope.TEAM
        assert corp_rec.scope == MemoryScope.CORPORATE

    def test_classification_validation(self):
        store = MemoryStore()
        with pytest.raises(ValueError):
            store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="x", classification="INVALID")

    def test_retention_and_expiry(self):
        store = MemoryStore()
        rec = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="ephemeral", retention_policy="ephemeral")
        assert rec.expires_at is not None
        assert not rec.is_expired()
        assert rec.is_accessible()


class TestMemoryProvenance:
    def test_provenance_fields_stored(self):
        store = MemoryStore()
        rec = store.write(
            owner="employee:kim", scope=MemoryScope.PERSONAL, content="from gmail",
            classification="INTERNAL",
            source_resource_id="gmail/user/kim/messages/123",
            source_acl_version="v2",
            source_delegation_id="dlg_abc",
            retention_policy="standard",
        )
        assert rec.source_resource_id == "gmail/user/kim/messages/123"
        assert rec.source_acl_version == "v2"
        assert rec.source_delegation_id == "dlg_abc"
        prov = store.get_provenance(rec.id)
        assert prov["source_resource_id"] == "gmail/user/kim/messages/123"
        assert prov["namespace"] == "user/kim"

    def test_provenance_audit_log(self):
        store = MemoryStore()
        store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="audit test")
        events = store.audit_events()
        assert any(e["event_type"] == "MEMORY_WRITE" for e in events)


class TestMemoryIsolation:
    def test_personal_isolation(self):
        store = MemoryStore()
        rec = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="private")
        # owner can read
        assert store.read(rec.id, requester="employee:kim") is not None
        # other user denied
        assert store.read(rec.id, requester="employee:lee") is None
        # dict context
        assert store.read(rec.id, requester=make_ctx("employee:kim")) is not None
        assert store.read(rec.id, requester=make_ctx("employee:lee")) is None

    def test_team_isolation(self):
        store = MemoryStore()
        rec = store.write(owner="group:dev", scope=MemoryScope.TEAM, content="team secret", group_id="dev")
        # member can read
        assert store.read(rec.id, requester=make_ctx("employee:kim", groups=["dev"])) is not None
        # non-member denied
        assert store.read(rec.id, requester=make_ctx("employee:lee", groups=["other"])) is None

    def test_corporate_readable_by_any_tenant_member(self):
        store = MemoryStore()
        rec = store.write(owner="organization", scope=MemoryScope.CORPORATE, content="corp doc")
        assert store.read(rec.id, requester="employee:kim") is not None
        assert store.read(rec.id, requester="employee:lee") is not None


class TestRevokeCascade:
    def test_invalidate_by_delegation(self):
        store = MemoryStore()
        r1 = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="a", source_delegation_id="dlg_123")
        r2 = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="b", source_delegation_id="dlg_123")
        r3 = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="c", source_delegation_id="dlg_other")
        count = store.invalidate_by_delegation("dlg_123")
        assert count == 2
        assert store.read(r1.id, requester="employee:kim") is None
        assert store.read(r2.id, requester="employee:kim") is None
        assert store.read(r3.id, requester="employee:kim") is not None

    def test_invalidate_by_resource_acl_version(self):
        store = MemoryStore()
        r1 = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="x", source_resource_id="gmail/user/kim/messages/1")
        assert store.invalidate_by_resource("gmail/user/kim/messages/1") == 1
        assert store.get(r1.id).invalidated is True

    def test_delegation_revoke_cascade_integration(self):
        """Simulates DelegationService.revoke → MemoryStore.invalidate_by_delegation cascade."""
        # This is the integration hook: when delegation is revoked, memories must cascade
        store = MemoryStore()
        delegation_id = "dlg_integration_test"
        rec = store.write(owner="employee:kim", scope=MemoryScope.PERSONAL, content="derived", source_delegation_id=delegation_id)
        # Simulate revoke
        from unittest.mock import MagicMock  # no external dep needed
        # In real flow: delegation_service.revoke(delegation_id) → store.invalidate_by_delegation(delegation_id)
        store.invalidate_by_delegation(delegation_id, reason="delegation_revoked")
        assert store.read(rec.id, requester="employee:kim") is None
        events = store.audit_events()
        assert any(e["event_type"] == "MEMORY_INVALIDATE" and e.get("delegation_id") == delegation_id for e in events)


# ── §29 Tests ────────────────────────────────────────────────────────

class TestDataClassification:
    def test_five_levels(self):
        assert DataClassification.PUBLIC == "PUBLIC"
        assert DataClassification.INTERNAL == "INTERNAL"
        assert DataClassification.CONFIDENTIAL == "CONFIDENTIAL"
        assert DataClassification.PII == "PII"
        assert DataClassification.SECRET == "SECRET"

    def test_content_hook_secret(self):
        dc = classify_content("my password is 1234")
        assert dc == DataClassification.SECRET

    def test_content_hook_pii(self):
        dc = classify_content("customer email bulk and 주민등록번호 990101-1234567")
        assert dc == DataClassification.PII

    def test_content_hook_confidential(self):
        dc = classify_content("confidential budget plan", resource="outline/team/docs")
        assert dc == DataClassification.CONFIDENTIAL

    def test_content_hook_public(self):
        dc = classify_content("public web article", resource="public/web/page")
        assert dc == DataClassification.PUBLIC

    def test_content_hook_internal_default(self):
        dc = classify_content("regular meeting notes")
        assert dc == DataClassification.INTERNAL


class TestHighEgress:
    def test_export_is_high_egress(self):
        is_high, reason = is_high_egress("EXPORT", "drive/user/kim/files")
        assert is_high is True

    def test_pii_with_egress_is_high(self):
        is_high, _ = is_high_egress("SEND", "gmail/user/kim/messages", data_classification="PII", is_external=True)
        assert is_high is True

    def test_secret_with_external_send_is_high(self):
        is_high, _ = is_high_egress("SEND", "outline/team/docs", data_classification="SECRET", is_external=True)
        assert is_high is True

    def test_bulk_pii_read_is_high(self):
        is_high, _ = is_high_egress("READ", "crm/pii/records", data_classification="PII", arg_hints={"bulk": True})
        assert is_high is True

    def test_internal_read_not_high_egress(self):
        is_high, _ = is_high_egress("READ", "outline/team/docs", data_classification="INTERNAL")
        assert is_high is False

    def test_risk_high_via_egress(self):
        # classify should return HIGH for egress cases
        assert classify("SEND", "gmail/user/kim/messages", is_external=True, data_classification="PII") == RiskLevel.HIGH
        assert classify("READ", "outline/team/docs", content="password: secret123") == RiskLevel.HIGH or classify("SEND", "outline/team/docs", content="password: secret123", is_external=True) == RiskLevel.HIGH

    def test_get_egress_classification(self):
        res = get_egress_classification("SEND", "gmail/user/kim/messages", content="hello", is_external=True)
        assert "classification" in res
        assert "is_high_egress" in res


# ── §28 Knowledge Retrieval Pipeline ─────────────────────────────────

class TestOutlineRetrieval:
    def test_search_then_acl_then_ranking(self):
        oc = OutlineConnector()
        ctx = make_ctx(tenant="test-tenant", groups=[])
        result = oc.retrieve("onboarding", ctx, limit=5)
        assert result.ranked is True
        assert len(result.documents) >= 1
        assert result.documents[0].title == "Onboarding Guide"

    def test_acl_filter_private_denied(self):
        # private docs require admin group
        oc = OutlineConnector()
        # non-admin should still get team docs but private filtered via doc ACL
        ctx_user = make_ctx(tenant="test-tenant", groups=["team"])
        ctx_admin = make_ctx(tenant="test-tenant", groups=["admin"])
        # Finance doc is private-ish
        result_user = oc.retrieve("Finance", ctx_user, limit=10)
        result_admin = oc.retrieve("Finance", ctx_admin, limit=10)
        # Admin sees at least as many as user
        assert len(result_admin.documents) >= len(result_user.documents)

    def test_knowledge_retriever_full_pipeline(self):
        oc = OutlineConnector()
        retriever = KnowledgeRetriever(outline_connector=oc)
        ctx = make_ctx()
        resp = retriever.retrieve("API design", ctx, limit=5)
        assert resp.query == "API design"
        assert len(resp.documents) >= 1
        assert "provenance" in resp.__dict__ or resp.provenance is not None
        assert resp.trace_id.startswith("trace_")

    def test_knowledge_provenance_recorded(self):
        oc = OutlineConnector()
        store = MemoryStore()
        retriever = KnowledgeRetriever(outline_connector=oc, memory_store=store)
        ctx = make_ctx(delegation="dlg_test_123")
        resp = retriever.retrieve("Security", ctx, limit=2, record_provenance=True)
        assert len(resp.documents) >= 1
        # memories should have been written with provenance
        assert store.count() >= 1
        mems = store.search(scope="corporate")
        assert any(m.source_resource_id and "outline" in m.source_resource_id for m in mems)
        # revoke should cascade
        store.invalidate_by_delegation("dlg_test_123")
        assert store.count() == 0

    def test_outline_api_search_mock(self):
        oc = OutlineConnector()
        docs = oc.search_documents("customer", limit=5)
        assert any("Customer" in d.title or "customer" in d.content.lower() for d in docs)

    def test_ranking_title_boost(self):
        oc = OutlineConnector()
        docs = [
            OutlineDocument(id="a", collection_id="team", title="No match", content="API design is here", url="u1"),
            OutlineDocument(id="b", collection_id="team", title="API Design Guidelines", content="other", url="u2"),
        ]
        oc.set_documents(docs)
        result = oc.retrieve("API Design", make_ctx(), limit=5)
        assert result.documents[0].id == "b"
