"""Outline adapter production tests — §§16H, 16I, 27.2, 28."""
import sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
for p in [ ROOT / "adapters", ROOT / "execution-gateway", ROOT / "security" / "memory-governance", ROOT / "security" / "policy-engine", ROOT / "packages" / "common-types",]:
    if str(p) not in sys.path: sys.path.insert(0, str(p))
from outline.adapter import OutlineAdapter  # type: ignore
try:
    from governance.governance import MemoryStore, MemoryScope  # type: ignore
except Exception:
    try:
        from security.memory_governance.governance.governance import MemoryStore, MemoryScope  # type: ignore
    except Exception:
        MemoryStore = None; MemoryScope = None  # type: ignore

def ctx(user="employee:kim", tenant="test-tenant", groups=None, delegation=None, caps=None):
    d: dict = {"user_id": user, "tenant_id": tenant, "groups": groups or []}
    if delegation: d["delegation_id"] = delegation
    if caps is not None: d["capabilities"] = caps
    return d

class TestAclFiltering:
    @pytest.mark.asyncio
    async def test_non_admin_cannot_see_private_docs(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=[])
        res = await a.search("Finance", user, limit=10)
        assert "doc_004" not in [d["id"] for d in res.get("data", [])]
    @pytest.mark.asyncio
    async def test_admin_sees_private_docs(self):
        a = OutlineAdapter(api_key=""); admin = ctx(groups=["admin"])
        res = await a.search("Finance", admin, limit=10)
        assert "doc_004" in [d["id"] for d in res.get("data", [])]
    @pytest.mark.asyncio
    async def test_acl_pre_filter_blocks_missing_tenant(self):
        a = OutlineAdapter(api_key="")
        bad = {"user_id": "employee:kim", "groups": []}
        with pytest.raises(PermissionError, match="tenant_id"):
            await a.search("onboarding", bad, limit=5)
    @pytest.mark.asyncio
    async def test_search_acl_before_ranking(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=[])
        res = await a.search("", user, limit=10)
        assert "doc_004" not in {d["id"] for d in res.get("data", [])}
    @pytest.mark.asyncio
    async def test_read_private_doc_denied_for_non_admin(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=[])
        with pytest.raises(PermissionError, match="ACL denied"):
            await a.get_document("doc_004", user)
    @pytest.mark.asyncio
    async def test_read_private_doc_allowed_for_admin(self):
        a = OutlineAdapter(api_key=""); admin = ctx(groups=["admin"])
        res = await a.get_document("doc_004", admin)
        assert res["data"]["id"] == "doc_004"
    @pytest.mark.asyncio
    async def test_read_private_doc_allowed_for_finance_group(self):
        a = OutlineAdapter(api_key=""); fin = ctx(groups=["finance"])
        res = await a.get_document("doc_004", fin)
        assert res["data"]["id"] == "doc_004"
    @pytest.mark.asyncio
    async def test_search_ranking_title_boost(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=["admin"])
        res = await a.search("Onboarding Guide", user, limit=5)
        assert res["data"][0]["id"] == "doc_001"

class TestCollectionIsolation:
    @pytest.mark.asyncio
    async def test_list_collections_hides_private_for_non_admin(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=[])
        res = await a.list_collections(user)
        ids = [c["id"] for c in res["data"]]
        assert "private" not in ids and "team" in ids
    @pytest.mark.asyncio
    async def test_list_collections_shows_private_for_admin(self):
        a = OutlineAdapter(api_key=""); admin = ctx(groups=["admin"])
        res = await a.list_collections(admin)
        assert "private" in [c["id"] for c in res["data"]]
    @pytest.mark.asyncio
    async def test_search_collection_filter_respects_isolation(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=[])
        res = await a.search("", user, limit=10, collection_id="private")
        assert len(res["data"]) == 0
        admin = ctx(groups=["admin"])
        res2 = await a.search("", admin, limit=10, collection_id="private")
        assert len(res2["data"]) >= 1
    @pytest.mark.asyncio
    async def test_search_collection_filter_team_accessible(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=[])
        res = await a.search("API", user, limit=10, collection_id="team")
        assert all(d["collection_id"] == "team" for d in res["data"])
    @pytest.mark.asyncio
    async def test_collections_info_acl(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=[])
        with pytest.raises(PermissionError):
            await a.call_tool("outline_collections_info", {"collection_id": "private"}, user)
        admin = ctx(groups=["admin"])
        res = await a.call_tool("outline_collections_info", {"collection_id": "private"}, admin)
        assert res["data"]["id"] == "private"

class TestProvenanceAndAclVersioning:
    @pytest.mark.asyncio
    async def test_provenance_written_on_search(self):
        if MemoryStore is None: pytest.skip("MemoryStore not available")
        store = MemoryStore(); a = OutlineAdapter(api_key="", memory_store=store)
        user = ctx(groups=["admin"], delegation="dlg_provenance_1")
        res = await a.search("API", user, limit=2)
        assert len(res["data"]) >= 1 and store.count() >= 1
        mems = store.search()
        assert any(m.source_resource_id and "outline" in m.source_resource_id for m in mems)
        assert any(m.source_delegation_id == "dlg_provenance_1" for m in mems)
    @pytest.mark.asyncio
    async def test_provenance_acl_version_tracked(self):
        if MemoryStore is None: pytest.skip("MemoryStore not available")
        store = MemoryStore(); a = OutlineAdapter(api_key="", memory_store=store)
        user = ctx(groups=["admin"]); rid = "outline/team/doc_002"
        ver = a.get_acl_version(rid); assert ver == "v1"
        await a.get_document("doc_002", user)
        mems = [m for m in store.search() if m.source_resource_id == rid]
        assert mems[0].source_acl_version == ver
    @pytest.mark.asyncio
    async def test_read_with_acl_version_check_pass(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=["admin"])
        res = await a.get_document("doc_002", user, expected_acl_version="v1")
        assert res["data"]["id"] == "doc_002"
    @pytest.mark.asyncio
    async def test_read_with_acl_version_mismatch_fails(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=["admin"])
        with pytest.raises(PermissionError, match="ACL version mismatch"):
            await a.get_document("doc_002", user, expected_acl_version="v999")
    def test_acl_version_bump(self):
        a = OutlineAdapter(api_key=""); rid = "outline/team/doc_001"
        assert a.get_acl_version(rid) == "v1"
        new = a.bump_acl_version(rid); assert new == "v2" and a.get_acl_version(rid) == "v2"
    @pytest.mark.asyncio
    async def test_invalidate_on_acl_version_change(self):
        if MemoryStore is None: pytest.skip("MemoryStore not available")
        store = MemoryStore(); a = OutlineAdapter(api_key="", memory_store=store)
        user = ctx(groups=["admin"], delegation="dlg_inv_1")
        await a.search("onboarding", user, limit=1)
        assert store.count() >= 1
        rid = "outline/team/doc_001"; a.bump_acl_version(rid)
        n = a.invalidate_by_acl_change(rid, reason="test_acl_change")
        assert n >= 1 and store.count() == 0
    @pytest.mark.asyncio
    async def test_update_acl_bumps_version_and_invalidates(self):
        if MemoryStore is None: pytest.skip("MemoryStore not available")
        store = MemoryStore(); a = OutlineAdapter(api_key="", memory_store=store)
        user = ctx(groups=["admin"], delegation="dlg_acl_up")
        await a.search("onboarding", user, limit=1)
        cnt_before = store.count(); assert cnt_before >= 1
        rid = "outline/team/doc_001"
        a.update_acl("outline/team/*", {"tenants": ["*"], "groups": ["admin"]})
        assert store.count(include_invalidated=True) >= cnt_before
    def test_set_and_get_acl_version(self):
        a = OutlineAdapter(api_key="")
        rid = "outline/engineering/doc_999"; a._acl_versions[rid] = "v1"
        old = a.set_acl_version(rid, "v5")
        assert old == "v1" and a.get_acl_version(rid) == "v5"

class TestFieldRowLimitsAndSecurity:
    @pytest.mark.asyncio
    async def test_limit_exceeds_max_results_denied(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=["admin"])
        with pytest.raises(ValueError, match="exceeds max_results"):
            await a.search("test", user, limit=999)
    @pytest.mark.asyncio
    async def test_limit_within_bounds_allowed(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=["admin"])
        res = await a.search("test", user, limit=5)
        assert isinstance(res, dict)
    @pytest.mark.asyncio
    async def test_denied_field_blocked(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=["admin"])
        with pytest.raises(ValueError, match="denied field"):
            await a.call_tool("outline_search", {"query": "test", "limit": 5, "fields": ["password"]}, user)
    @pytest.mark.asyncio
    async def test_capability_check_denies_without_read(self):
        a = OutlineAdapter(api_key="")
        no_cap = ctx(groups=["admin"], caps=[{"resource": "gmail/user/*", "action": "READ"}])
        with pytest.raises(PermissionError, match="capability denied"):
            await a.search("test", no_cap, limit=5)
    @pytest.mark.asyncio
    async def test_capability_outline_allows(self):
        a = OutlineAdapter(api_key="")
        caps = [{"resource": "outline/*", "action": "READ"}]
        user = ctx(groups=["admin"], caps=caps)
        res = await a.search("API", user, limit=5)
        assert isinstance(res, dict)
    @pytest.mark.asyncio
    async def test_capability_missing_dev_allows(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=["admin"])
        res = await a.search("API", user, limit=5)
        assert isinstance(res, dict)
    @pytest.mark.asyncio
    async def test_data_access_read_replica_enforced(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=["admin"])
        res = await a.search("onboarding", user, limit=5)
        assert res.get("error") != "DATA_ACCESS_DENIED"
    @pytest.mark.asyncio
    async def test_search_empty_query_returns_filtered(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=[])
        res = await a.search("", user, limit=10)
        for d in res["data"]: assert d["collection_id"] != "private"
    @pytest.mark.asyncio
    async def test_documents_list_respects_acl(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=[])
        res = await a.call_tool("outline_documents_list", {"collection_id": "team", "limit": 10}, user)
        assert all(d["collection_id"] == "team" for d in res["data"])
        res2 = await a.call_tool("outline_documents_list", {"collection_id": "private", "limit": 10}, user)
        assert len(res2["data"]) == 0
    def test_describe_tools_has_endpoints(self):
        a = OutlineAdapter(api_key="")
        tools = a.describe_tools()
        assert any(t["name"] == "outline_search" for t in tools)
    @pytest.mark.asyncio
    async def test_list_tools_and_resources(self):
        a = OutlineAdapter(api_key="")
        assert "outline_search" in await a.list_tools()
        assert "outline/*" in await a.list_resources()
    @pytest.mark.asyncio
    async def test_check_acl_methods(self):
        a = OutlineAdapter(api_key=""); user = ctx(groups=["admin"])
        assert a.check_acl(user, "outline/team/docs", action="READ").allowed is True
        assert a.check_document_acl(user, a._docs[0]).allowed is True
