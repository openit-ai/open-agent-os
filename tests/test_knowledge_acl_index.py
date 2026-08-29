"""Deterministic unit tests for Enterprise Knowledge Index ACL primitives.

Covers (as required):
 - allow / deny filtering
 - acl_version bump detection + invalidation before retrieval
 - resource deletion invalidates
 - revalidation (update version OR mark inaccessible)
 - no cross-tenant access
 - pre-retrieval filtering (no post-retrieval substitute)
 - source ACL is source of truth

Strict TDD: tests drive the knowledge_index.acl module under
knowledge_index/acl.py — no production server, no DB.
"""
from __future__ import annotations

import pytest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KI = ROOT / "knowledge_index"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(KI.parent) not in sys.path:
    sys.path.insert(0, str(KI.parent))

from knowledge_index.acl import KnowledgeACLIndex, evaluate_acl, SourceState


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _idx() -> KnowledgeACLIndex:
    return KnowledgeACLIndex()

def _seed_resource(idx: KnowledgeACLIndex, tenant="tenant_a", resource="outline/team/doc_001",
                   collection="team", acl_version="v1", groups=("team",), users=(), n_chunks=2):
    chunks=[]
    for i in range(n_chunks):
        chunks.append({"chunk_id": f"chk_{i+1}", "content": f"hello world chunk {i+1} onboarding"})
    return idx.bulk_index(tenant_id=tenant, resource_id=resource, collection_id=collection,
                          acl_version=acl_version, chunks=chunks,
                          allowed_groups=list(groups), allowed_users=list(users))


# ---------------------------------------------------------------------------
# ACL allow / deny
# ---------------------------------------------------------------------------

class TestACLAllowDeny:
    def test_allow_member_group(self):
        idx = _idx()
        _seed_resource(idx, groups=("eng",))
        res = idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=["eng"], query="")
        assert len(res) == 2

    def test_deny_non_member(self):
        idx = _idx()
        _seed_resource(idx, groups=("eng",))
        res = idx.search(tenant_id="tenant_a", user_id="employee:lee", groups=["marketing"], query="")
        assert len(res) == 0

    def test_allow_specific_user(self):
        idx = _idx()
        _seed_resource(idx, groups=(), users=("employee:kim",))
        assert len(idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="")) == 2
        assert len(idx.search(tenant_id="tenant_a", user_id="employee:lee", groups=[], query="")) == 0

    def test_wildcard_group_allows_any(self):
        idx = _idx()
        _seed_resource(idx, groups=("*",))
        assert len(idx.search(tenant_id="tenant_a", user_id="employee:anyone", groups=[], query="")) == 2

    def test_no_restriction_means_tenant_wide(self):
        idx = _idx()
        _seed_resource(idx, groups=(), users=())
        assert len(idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="")) == 2

    def test_evaluate_acl_pure_function(self):
        assert evaluate_acl(("eng",), (), "employee:kim", ["eng"]) is True
        assert evaluate_acl(("eng",), (), "employee:kim", ["other"]) is False
        assert evaluate_acl(("*",), (), "employee:kim", []) is True
        assert evaluate_acl((), (), "employee:kim", []) is True

    def test_collection_pre_filter(self):
        idx = _idx()
        _seed_resource(idx, resource="outline/team/doc_001", collection="team", groups=("*",))
        _seed_resource(idx, resource="outline/private/doc_002", collection="private", groups=("*",))
        team_only = idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], collection_id="team", query="")
        assert all(c.collection_id == "team" for c in team_only)
        assert len(team_only) == 2
        private_only = idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], collection_id="private", query="")
        assert len(private_only) == 2

    def test_missing_tenant_fails_closed(self):
        idx = _idx()
        _seed_resource(idx)
        with pytest.raises(ValueError, match="tenant_id"):
            idx.search(tenant_id="", user_id="employee:kim", groups=[], query="")

    def test_denied_never_visible_even_with_matching_query(self):
        idx = _idx()
        _seed_resource(idx, groups=("admin",))
        res = idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="hello world")
        assert len(res) == 0  # not hidden after — never returned


# ---------------------------------------------------------------------------
# version bump detection + invalidation before retrieval
# ---------------------------------------------------------------------------

class TestVersionBumpInvalidation:
    def test_detect_version_change_true_on_bump(self):
        idx = _idx()
        _seed_resource(idx, acl_version="v1")
        assert idx.detect_version_change("tenant_a", "outline/team/doc_001", "v1") is False
        assert idx.detect_version_change("tenant_a", "outline/team/doc_001", "v2") is True

    def test_detect_no_chunks_false(self):
        idx = _idx()
        assert idx.detect_version_change("tenant_a", "nonexistent", "v2") is False

    def test_detect_deleted_when_source_reports_none(self):
        idx = _idx()
        _seed_resource(idx)
        assert idx.detect_version_change("tenant_a", "outline/team/doc_001", None) is True

    def test_invalidate_stale_before_retrieval(self):
        idx = _idx()
        _seed_resource(idx, acl_version="v1", groups=("*",))
        # simulate source bump to v2 before search
        assert idx.detect_version_change("tenant_a", "outline/team/doc_001", "v2") is True
        result = idx.invalidate_stale("tenant_a", "outline/team/doc_001", "v2")
        assert result.invalidated_count == 2
        # after invalidation, old chunks not retrievable
        assert idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="") == []
        # live count zero, total still 2 (auditable)
        assert idx.count_live("tenant_a") == 0
        assert idx.count_all("tenant_a") == 2

    def test_auto_invalidate_via_search(self):
        idx = _idx()
        _seed_resource(idx, acl_version="v1", groups=("*",))
        # provide source_versions dict — search auto-invalidates stale before retrieval
        res = idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[],
                         query="", source_versions={"outline/team/doc_001": "v2"})
        assert len(res) == 0
        assert idx.count_live("tenant_a") == 0

    def test_no_invalidation_when_version_unchanged(self):
        idx = _idx()
        _seed_resource(idx, acl_version="v1", groups=("*",))
        res = idx.invalidate_stale("tenant_a", "outline/team/doc_001", "v1")
        assert res.invalidated_count == 0
        assert len(idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="")) == 2

    def test_source_is_truth_over_index(self):
        idx = _idx()
        _seed_resource(idx, acl_version="v1", groups=("*",))
        # even though index says v1, source says v5 -> old must go
        idx.invalidate_stale("tenant_a", "outline/team/doc_001", "v5")
        assert idx.current_indexed_version("tenant_a", "outline/team/doc_001") is None
        # re-index with new truth
        _seed_resource(idx, acl_version="v5", groups=("*",), n_chunks=1)
        # but chunk_id collides; live is now the new one(s)
        # after re-index at least one live at v5
        assert idx.count_live("tenant_a") >= 1


# ---------------------------------------------------------------------------
# resource deletion invalidates
# ---------------------------------------------------------------------------

class TestDeletionInvalidation:
    def test_delete_invalidates_all_live(self):
        idx = _idx()
        _seed_resource(idx, groups=("*",))
        assert idx.count_live("tenant_a") == 2
        r = idx.invalidate_deleted("tenant_a", "outline/team/doc_001")
        assert r.invalidated_count == 2
        assert idx.count_live("tenant_a") == 0
        assert idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="") == []

    def test_delete_idempotent_second_call_zero(self):
        idx = _idx()
        _seed_resource(idx, groups=("*",))
        idx.invalidate_deleted("tenant_a", "outline/team/doc_001")
        r2 = idx.invalidate_deleted("tenant_a", "outline/team/doc_001")
        assert r2.invalidated_count == 0

    def test_deleted_resource_not_returned_even_for_admin(self):
        idx = _idx()
        _seed_resource(idx, groups=("admin",))
        idx.invalidate_deleted("tenant_a", "outline/team/doc_001")
        assert idx.search(tenant_id="tenant_a", user_id="employee:admin", groups=["admin"], query="") == []

    def test_deletion_does_not_affect_other_resources(self):
        idx = _idx()
        _seed_resource(idx, resource="outline/team/doc_001", groups=("*",))
        _seed_resource(idx, resource="outline/team/doc_002", groups=("*",))
        idx.invalidate_deleted("tenant_a", "outline/team/doc_001")
        res = idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="")
        assert all(c.resource_id == "outline/team/doc_002" for c in res)
        assert len(res) == 2

    def test_deletion_does_not_affect_other_tenant_same_resource(self):
        idx = _idx()
        _seed_resource(idx, tenant="tenant_a", resource="outline/team/doc_001", groups=("*",))
        _seed_resource(idx, tenant="tenant_b", resource="outline/team/doc_001", groups=("*",))
        idx.invalidate_deleted("tenant_a", "outline/team/doc_001")
        assert len(idx.search(tenant_id="tenant_b", user_id="employee:kim", groups=[], query="")) == 2
        assert len(idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="")) == 0


# ---------------------------------------------------------------------------
# revalidation: update version OR mark inaccessible
# ---------------------------------------------------------------------------

class TestRevalidation:
    def test_revalidate_bumps_version(self):
        idx = _idx()
        _seed_resource(idx, acl_version="v1", groups=("*",))
        r = idx.revalidate("tenant_a", "outline/team/doc_001", new_acl_version="v2")
        assert r.status == "updated"
        assert r.updated_count == 2
        assert idx.current_indexed_version("tenant_a", "outline/team/doc_001") == "v2"
        # still retrievable after bump (still accessible)
        assert len(idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="")) == 2

    def test_revalidate_same_version_no_change(self):
        idx = _idx()
        _seed_resource(idx, acl_version="v1", groups=("*",))
        r = idx.revalidate("tenant_a", "outline/team/doc_001", new_acl_version="v1")
        assert r.status == "no_change"
        assert len(idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="")) == 2

    def test_revalidate_marks_inaccessible(self):
        idx = _idx()
        _seed_resource(idx, acl_version="v1", groups=("*",))
        r = idx.revalidate("tenant_a", "outline/team/doc_001", new_acl_version="v2", is_inaccessible=True)
        assert r.status == "inaccessible"
        assert r.invalidated_count == 2
        assert idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="") == []

    def test_revalidate_deleted(self):
        idx = _idx()
        _seed_resource(idx, groups=("*",))
        r = idx.revalidate("tenant_a", "outline/team/doc_001", new_acl_version=None, is_deleted=True)
        assert r.status == "deleted"
        assert r.invalidated_count == 2
        assert idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="") == []

    def test_revalidate_from_source_state_exists(self):
        idx = _idx()
        _seed_resource(idx, acl_version="v1", groups=("team",))
        state = SourceState(resource_id="outline/team/doc_001", tenant_id="tenant_a",
                            acl_version="v2", exists=True, allowed_groups=("admin",), collection_id="team")
        r = idx.revalidate_from_source(state)
        assert r.status == "updated"
        # now only admin can see
        assert len(idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=["team"], query="")) == 0
        assert len(idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=["admin"], query="")) == 2

    def test_revalidate_from_source_state_deleted(self):
        idx = _idx()
        _seed_resource(idx, groups=("*",))
        state = SourceState(resource_id="outline/team/doc_001", tenant_id="tenant_a",
                            acl_version=None, exists=False)
        r = idx.revalidate_from_source(state)
        assert r.status == "deleted"
        assert idx.count_live("tenant_a") == 0

    def test_revalidate_not_found(self):
        idx = _idx()
        r = idx.revalidate("tenant_a", "outline/team/missing", new_acl_version="v1")
        assert r.status == "not_found"

    def test_revalidate_updates_acl_sets(self):
        idx = _idx()
        _seed_resource(idx, acl_version="v1", groups=("team",))
        idx.revalidate("tenant_a", "outline/team/doc_001", new_acl_version="v2",
                       new_allowed_groups=["admin"], new_allowed_users=[])
        assert len(idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=["team"], query="")) == 0
        assert len(idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=["admin"], query="")) == 2


# ---------------------------------------------------------------------------
# no cross-tenant access
# ---------------------------------------------------------------------------

class TestNoCrossTenant:
    def test_tenant_isolation_strict(self):
        idx = _idx()
        _seed_resource(idx, tenant="tenant_a", resource="outline/team/doc_001", groups=("*",))
        _seed_resource(idx, tenant="tenant_b", resource="outline/team/doc_001", groups=("*",))
        a = idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="")
        b = idx.search(tenant_id="tenant_b", user_id="employee:kim", groups=[], query="")
        assert all(c.tenant_id == "tenant_a" for c in a)
        assert all(c.tenant_id == "tenant_b" for c in b)
        assert len(a) == 2 and len(b) == 2

    def test_tenant_a_cannot_see_b_even_with_same_groups(self):
        idx = _idx()
        _seed_resource(idx, tenant="tenant_b", groups=("admin",))
        res = idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=["admin"], query="")
        assert len(res) == 0

    def test_version_bump_and_delete_scoped_to_tenant(self):
        idx = _idx()
        _seed_resource(idx, tenant="tenant_a", resource="outline/team/doc_001", acl_version="v1", groups=("*",))
        _seed_resource(idx, tenant="tenant_b", resource="outline/team/doc_001", acl_version="v1", groups=("*",))
        idx.invalidate_stale("tenant_a", "outline/team/doc_001", "v2")
        assert idx.count_live("tenant_a") == 0
        assert idx.count_live("tenant_b") == 2

    def test_index_requires_tenant(self):
        idx = _idx()
        with pytest.raises(ValueError, match="tenant_id"):
            idx.index_chunk(tenant_id="", resource_id="r1", collection_id="team",
                            chunk_id="c1", acl_version="v1")

    def test_mixed_tenant_query_never_leaks(self):
        idx = _idx()
        for tenant in ("tenant_a", "tenant_b", "tenant_c"):
            _seed_resource(idx, tenant=tenant, resource=f"outline/team/doc_{tenant}", groups=("*",))
        for tenant in ("tenant_a", "tenant_b", "tenant_c"):
            res = idx.search(tenant_id=tenant, user_id="employee:kim", groups=[], query="")
            assert all(c.tenant_id == tenant for c in res), f"leak into {tenant}"


# ---------------------------------------------------------------------------
# pre-retrieval invariant (no post-filter substitute)
# ---------------------------------------------------------------------------

class TestPreRetrievalInvariant:
    def test_search_never_returns_invalidated(self):
        idx = _idx()
        _seed_resource(idx, groups=("*",))
        # manually mark invalidated without calling search helper — ensure search hides it
        for rec in idx.get_live_chunks("tenant_a", "outline/team/doc_001"):
            rec.invalidated = True
        assert idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="") == []

    def test_search_never_returns_inaccessible_after_revalidate(self):
        idx = _idx()
        _seed_resource(idx, groups=("*",))
        idx.revalidate("tenant_a", "outline/team/doc_001", new_acl_version="v2", is_inaccessible=True)
        assert idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="") == []

    def test_acl_is_pre_filter_not_post_hide(self):
        """Denied chunks must be excluded before ranking/query — absence proves pre-filter."""
        idx = _idx()
        # two resources: one allowed, one denied; query matches both
        _seed_resource(idx, resource="outline/team/allowed", groups=("*",),
                       n_chunks=1, acl_version="v1")
        # overwrite content to be distinct
        for rec in idx.get_live_chunks("tenant_a", "outline/team/allowed"):
            rec.content = "secret onboarding allowed"
        _seed_resource(idx, resource="outline/private/denied", collection="private",
                       groups=("admin",), n_chunks=1, acl_version="v1")
        for rec in idx.get_live_chunks("tenant_a", "outline/private/denied"):
            rec.content = "secret onboarding denied"
        # non-admin queries onboarding — only allowed should appear
        res = idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="onboarding")
        assert len(res) == 1
        assert res[0].resource_id == "outline/team/allowed"
        # admin sees both
        res_admin = idx.search(tenant_id="tenant_a", user_id="employee:admin", groups=["admin"], query="onboarding")
        ids = {c.resource_id for c in res_admin}
        assert "outline/private/denied" in ids and "outline/team/allowed" in ids

    def test_source_of_truth_precedence(self):
        """If source says v2 but index has v1, stale must be gone even if ACL would otherwise allow."""
        idx = _idx()
        _seed_resource(idx, groups=("*",), acl_version="v1")
        # allowed by ACL, but version stale — with source_versions auto-invalidate, result empty
        res = idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[],
                         query="", source_versions={"outline/team/doc_001": "v2"})
        assert res == []
        # source-of-truth wins: old chunks purged before retrieval
        assert idx.count_live("tenant_a") == 0


# ---------------------------------------------------------------------------
# edge / determinism
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_ordering_deterministic(self):
        idx = _idx()
        _seed_resource(idx, resource="outline/team/b", groups=("*",))
        _seed_resource(idx, resource="outline/team/a", groups=("*",))
        res = idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="")
        assert [c.resource_id for c in res] == sorted([c.resource_id for c in res])

    def test_evaluate_group_prefix_normalization(self):
        assert evaluate_acl(("eng",), (), "employee:kim", ["group:eng"]) is True
        assert evaluate_acl(("group:eng",), (), "employee:kim", ["eng"]) is True

    def test_empty_group_and_user_denied_when_restricted(self):
        idx = _idx()
        _seed_resource(idx, groups=("admin",), users=())
        assert idx.search(tenant_id="tenant_a", user_id="employee:kim", groups=[], query="") == []
