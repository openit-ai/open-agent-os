"""IAM adapter production tests — provider abstraction, sync, mapping, security_domain, deny precedence, deprovision."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for p in [ROOT / "adapters",ROOT / "control-plane",ROOT / "security" / "policy-engine",ROOT / "security" / "audit",ROOT / "security" / "delegation",ROOT / "security" / "approval",ROOT / "security" / "token",ROOT / "packages" / "policy-model",ROOT / "packages" / "audit-model",ROOT / "packages" / "delegation-model",ROOT / "packages" / "common-types",ROOT / "packages" / "agent-context"]:
    if str(p) not in sys.path: sys.path.insert(0, str(p))
if str(ROOT / "security") not in sys.path: sys.path.insert(0, str(ROOT / "security"))
import pytest
from iam.adapter import IamAdapter, GoogleWorkspaceProvider, EntraProvider, OidcProvider, _provider_factory
from policy_model import PolicyBundle, PolicyDecision, PolicyEvaluationRequest, PolicyRule, PolicySource, POLICY_EVALUATION_ORDER
from policy_engine.engine import PolicyEngine
from policy_engine.default_bundle import default_bundle
from delegation_service.service import DelegationService
from audit_ledger.ledger import AuditLedger
class TestProviderAbstraction:
    def test_google_provider(self):
        a = IamAdapter(provider="google", domain="example.com", api_key="")
        assert a.iam_provider == "google"
        assert isinstance(a._provider_impl, GoogleWorkspaceProvider)
        assert "admin.googleapis.com" in a._provider_impl.user_list_url()
    def test_entra_aliases(self):
        for prov in ["azure", "entra", "microsoft"]:
            a = IamAdapter(provider=prov, domain="example.com", api_key="k")
            assert isinstance(a._provider_impl, EntraProvider)
            assert "graph.microsoft.com" in a._provider_impl.user_list_url()
    def test_oidc_provider(self):
        for prov in ["okta", "oidc", "generic"]:
            a = IamAdapter(provider=prov, domain="example.com", api_key="tok")
            assert isinstance(a._provider_impl, OidcProvider)
    def test_provider_normalize_google(self):
        p = GoogleWorkspaceProvider(domain="example.com", api_key="k")
        raw = {"primaryEmail": "Kim@Example.COM", "id": "123", "name": {"fullName": "Kim Lee"}, "suspended": False}
        norm = p.normalize_user(raw)
        assert norm["email"] == "Kim@Example.COM"
        assert norm["id"] == "123"
        assert norm["provider"] == "google"
    def test_provider_normalize_entra(self):
        p = EntraProvider(domain="example.com", api_key="k")
        raw = {"mail": "lee@example.com", "id": "uid-9", "displayName": "Lee", "accountEnabled": True}
        norm = p.normalize_user(raw)
        assert norm["email"] == "lee@example.com"
        assert norm["display_name"] == "Lee"
        assert norm["provider"] == "entra"
    def test_provider_normalize_oidc(self):
        p = OidcProvider(domain="example.com", api_key="k")
        raw = {"sub": "user123", "email": "user@example.com", "name": "User", "groups": ["eng"]}
        norm = p.normalize_user(raw)
        assert norm["id"] == "user123"
        assert norm["email"] == "user@example.com"
        assert "eng" in norm["groups"]
    def test_provider_factory_unknown_defaults_google(self):
        p = _provider_factory("unknown-provider", "example.com", "")
        assert isinstance(p, GoogleWorkspaceProvider)
    def test_describe_includes_provider_impl(self):
        a = IamAdapter(provider="google", domain="acme.com", api_key="")
        d = a.describe()
        assert d["provider_impl"] == "google"
        assert d["tenant_id"] == "acme"
class TestPrincipalMapping:
    def test_email_to_employee_principal(self):
        a = IamAdapter(domain="example.com")
        assert a.to_employee_principal("Kim@example.com") == "employee:kim"
        assert a.to_employee_principal("Alice.Wu@example.com") == "employee:alice.wu"
        assert a.to_employee_principal("KIM") == "employee:kim"
        assert a.to_employee_principal("employee:park") == "employee:park"
    def test_sanitize_special_chars(self):
        a = IamAdapter()
        assert a.to_employee_principal("Kim@Open!@example.com") == "employee:kimopen"
        assert a.to_employee_principal("@@@") == "employee:unknown"
        assert a.to_employee_principal("") == "employee:unknown"
    def test_to_agent_principal(self):
        a = IamAdapter()
        assert a.to_agent_principal("employee:kim") == "agent:assistant:kim"
        with pytest.raises(ValueError):
            a.to_agent_principal("bad:kim")
    def test_resolve_principal_includes_tenant(self):
        a = IamAdapter(provider="google", domain="example.com", tenant_id="cotenant")
        r = a.resolve_principal("bob@example.com")
        assert r["employee_principal"] == "employee:bob"
        assert r["agent_principal"] == "agent:assistant:bob"
        assert r["tenant_id"] == "cotenant"
        assert r["provider"] == "google"
    def test_register_principal_overrides_derive(self):
        a = IamAdapter()
        a.register_principal("ext-123", "employee:park")
        r = a.resolve_principal("ext-123")
        assert r["employee_principal"] == "employee:park"
        with pytest.raises(ValueError):
            a.register_principal("x", "bad:kim")
    def test_resolve_principal_id_without_at(self):
        a = IamAdapter()
        r = a.resolve_principal("myuser")
        assert r["employee_principal"] == "employee:myuser"
class TestTenantAndSecurityDomain:
    def test_resolve_tenant_from_domain(self):
        a = IamAdapter(domain="acme.co.kr")
        assert a.resolve_tenant() == "acme"
        assert a.resolve_tenant("user@other.com") == "acme"
        b = IamAdapter(domain="", tenant_id="default")
        assert b.resolve_tenant("user@foo.bar.com") == "foo"
    def test_assign_security_domain_explicit(self):
        a = IamAdapter(domain="example.com")
        user = {"email": "kim@example.com", "security_domain": "finance", "groups": ["eng"]}
        assert a.assign_security_domain(user) == "finance"
    def test_assign_security_domain_group_heuristic(self):
        a = IamAdapter(domain="example.com")
        assert a.assign_security_domain({"email": "a@example.com", "groups": ["eng-team"]}) == "development"
        assert a.assign_security_domain({"email": "a@example.com", "groups": ["finance-team"]}) == "finance"
        assert a.assign_security_domain({"email": "a@example.com", "groups": ["HR-group"]}) == "hr"
    def test_assign_security_domain_org_unit(self):
        a = IamAdapter(domain="example.com")
        assert a.assign_security_domain({"email": "a@example.com", "org_unit": "/Engineering/Dev"}) == "development"
        assert a.assign_security_domain({"email": "a@example.com", "department": "Finance"}) == "finance"
    def test_assign_security_domain_default(self):
        a = IamAdapter(domain="example.com", default_security_domain="general")
        assert a.assign_security_domain({"email": "a@example.com", "groups": []}) == "general"
        b = IamAdapter(domain="example.com", default_security_domain="restricted")
        assert b.assign_security_domain({"email": "x@x.com"}) == "restricted"
    def test_configure_security_domain_override(self):
        a = IamAdapter(domain="example.com")
        a.configure_security_domain({"custom-group": "secret"})
        assert a.assign_security_domain({"email": "a@example.com", "groups": ["custom-group"]}) == "secret"
    def test_string_input_security_domain(self):
        a = IamAdapter(domain="example.com")
        assert a.assign_security_domain("nobody@example.com") == "general"
class TestSync:
    @pytest.mark.asyncio
    async def test_sync_users_basic(self):
        a = IamAdapter(provider="google", domain="example.com", api_key="")
        users = [{"primaryEmail": "kim@example.com", "id": "u1", "name": {"fullName": "Kim"}, "groups": ["eng"]}, {"primaryEmail": "lee@example.com", "id": "u2", "name": {"fullName": "Lee"}, "groups": ["finance"]}]
        out = await a.sync_users(users)
        assert out["synced"] == 2
        assert out["total"] >= 2
        u = await a.get_user("kim@example.com")
        assert u["employee_principal"] == "employee:kim"
        assert "tenant_id" in u
        assert "security_domain" in u
    @pytest.mark.asyncio
    async def test_sync_users_dedup_and_principal(self):
        a = IamAdapter(domain="example.com")
        await a.sync_users([{"id": "u1", "email": "park@example.com", "display_name": "Park", "groups": ["eng"]}])
        lst = await a.list_users()
        assert lst["_skeleton"] is True
        assert any(u.get("email") == "park@example.com" for u in lst["users"])
    @pytest.mark.asyncio
    async def test_sync_groups_and_membership(self):
        a = IamAdapter(domain="example.com")
        await a.sync_users([{"id": "u1", "email": "a@example.com", "groups": ["g1"]}])
        await a.sync_groups({"g1": ["a@example.com", "b@example.com"], "g2": ["c@example.com"]})
        g = await a.get_group("g1")
        assert "a@example.com" in g["members"]
        assert "b@example.com" in g["members"]
        lst = await a.list_groups()
        assert "g1" in lst["groups"]
        assert "g2" in lst["groups"]
    @pytest.mark.asyncio
    async def test_sync_user_groups_reverse_index(self):
        a = IamAdapter(domain="example.com")
        await a.sync_users([{"id": "u1", "email": "x@example.com", "groups": ["alpha", "beta"]}])
        assert "alpha" in a._user_groups["u1"]
        assert "x@example.com" in a._groups["alpha"]
    @pytest.mark.asyncio
    async def test_list_users_enrichment(self):
        a = IamAdapter(domain="example.com")
        await a.sync_users([{"id": "u10", "email": "enrich@example.com", "groups": ["eng"]}])
        out = await a.list_users(max_results=10)
        u = [x for x in out["users"] if x.get("email") == "enrich@example.com"][0]
        assert u["employee_principal"] == "employee:enrich"
        assert u["security_domain"] == "development"
class TestGroupPolicyBinding:
    def test_bind_group_policy_inline_rules(self):
        a = IamAdapter(domain="example.com", tenant_id="t1")
        rule = PolicyRule(id="r1", source=PolicySource.GROUP_GRANT, action="READ", resource_pattern="crm/*", effect=PolicyDecision.ALLOW)
        out = a.bind_group_policy("eng", rules=[rule])
        assert out["group_id"] == "eng"
        assert a.get_group_bundle("eng") is not None
    def test_bind_group_policy_with_bundle(self):
        a = IamAdapter(domain="example.com", tenant_id="t1")
        bundle = PolicyBundle(id="b1", tenant_id="t1", name="test", version="1.0", rules=[PolicyRule(id="r1", source=PolicySource.GROUP_GRANT, action="READ", resource_pattern="drive/*", effect=PolicyDecision.ALLOW)])
        out = a.bind_group_policy("g1", bundle=bundle, security_domain="development")
        assert out["security_domain"] == "development"
        assert a._security_domain_map["g1"] == "development"
    def test_bind_requires_bundle_or_rules(self):
        a = IamAdapter()
        with pytest.raises(ValueError):
            a.bind_group_policy("g1")
    def test_resolve_group_bundles(self):
        a = IamAdapter(domain="example.com")
        r = PolicyRule(id="r1", source=PolicySource.GROUP_GRANT, action="READ", resource_pattern="*internal*", effect=PolicyDecision.ALLOW)
        a.bind_group_policy("g1", rules=[r])
        bundles = a.resolve_group_bundles(["g1", "unknown"])
        assert len(bundles) == 1
    def test_build_policy_bundles_for_user(self):
        a = IamAdapter(domain="example.com", tenant_id="t1")
        r = PolicyRule(id="r1", source=PolicySource.GROUP_GRANT, action="READ", resource_pattern="crm/*", effect=PolicyDecision.ALLOW)
        a.bind_group_policy("eng", rules=[r])
        import asyncio
        asyncio.get_event_loop().run_until_complete(a.sync_groups({"eng": ["kim@example.com"]}))
        bundles = a.build_policy_bundles_for_user("employee:kim", groups=["eng"], tenant_id="t1")
        assert any(getattr(b, "id", "") == "group-bundle-eng" or "eng" in getattr(b, "id", "") for b in bundles)
        assert any(getattr(b, "id", "") == "default-bundle-v1" for b in bundles)
class TestPolicyPrecedence:
    def test_explicit_deny_overrides_group_allow(self):
        a = IamAdapter(domain="example.com", tenant_id="t1")
        allow_rule = PolicyRule(id="allow-crm", source=PolicySource.GROUP_GRANT, action="READ", resource_pattern="crm/*", effect=PolicyDecision.ALLOW)
        a.bind_group_policy("eng", rules=[allow_rule])
        deny_rule = PolicyRule(id="deny-crm-secret", source=PolicySource.EXPLICIT_DENY, action="READ", resource_pattern="crm/secret/*", effect=PolicyDecision.DENY)
        deny_bundle = PolicyBundle(id="deny-bundle", tenant_id="t1", name="deny", version="1.0", rules=[deny_rule])
        res = a.evaluate_access("employee:kim", "READ", "crm/secret/123", groups=["eng"], extra_bundles=[deny_bundle])
        assert res.decision == PolicyDecision.DENY
        assert res.source == PolicySource.EXPLICIT_DENY
    def test_group_allow_when_no_deny(self):
        a = IamAdapter(domain="example.com", tenant_id="t1")
        allow_rule = PolicyRule(id="allow-crm", source=PolicySource.GROUP_GRANT, action="READ", resource_pattern="crm/*", effect=PolicyDecision.ALLOW)
        a.bind_group_policy("eng", rules=[allow_rule])
        res = a.evaluate_access("employee:kim", "READ", "crm/public/123", groups=["eng"])
        assert res.decision == PolicyDecision.ALLOW
        assert res.source == PolicySource.GROUP_GRANT
    def test_default_bundle_fallback(self):
        a = IamAdapter(domain="example.com", tenant_id="t1")
        res = a.evaluate_access("employee:kim", "READ", "outline/doc/123", groups=[])
        assert res.decision == PolicyDecision.ALLOW
        assert res.source == PolicySource.DEFAULT_BUNDLE
    def test_default_deny_when_no_match(self):
        a = IamAdapter(domain="example.com", tenant_id="t1")
        res = a.evaluate_access("employee:kim", "DELETE", "unknown/resource/123", groups=[])
        assert res.decision == PolicyDecision.DENY
        assert res.source == PolicySource.DEFAULT_DENY
    def test_explicit_deny_over_personal_delegation(self):
        deny_rule = PolicyRule(id="deny-gmail", source=PolicySource.EXPLICIT_DENY, action="READ", resource_pattern="gmail/*", effect=PolicyDecision.DENY)
        personal_rule = PolicyRule(id="allow-gmail", source=PolicySource.PERSONAL_DELEGATION, action="READ", resource_pattern="gmail/*", effect=PolicyDecision.ALLOW)
        deny_bundle = PolicyBundle(id="b-deny", tenant_id="t1", name="deny", version="1.0", rules=[deny_rule])
        personal_bundle = PolicyBundle(id="b-personal", tenant_id="t1", name="personal", version="1.0", rules=[personal_rule])
        engine = PolicyEngine(bundles=[deny_bundle, personal_bundle])
        req = PolicyEvaluationRequest(tenant_id="t1", user_id="employee:kim", agent_id="agent:assistant:kim", action="READ", resource="gmail/user/kim/1")
        res = engine.evaluate(req)
        assert res.decision == PolicyDecision.DENY
        assert res.source == PolicySource.EXPLICIT_DENY
    def test_evaluation_order_is_strict(self):
        expected = [PolicySource.EXPLICIT_DENY,PolicySource.SECURITY_BOUNDARY_DENY,PolicySource.PERSONAL_DELEGATION,PolicySource.PERSISTENT_USER_GRANT,PolicySource.GROUP_GRANT,PolicySource.DEFAULT_BUNDLE,PolicySource.JIT_APPROVAL,PolicySource.DEFAULT_DENY]
        assert POLICY_EVALUATION_ORDER == expected
class TestJITGroupSync:
    @pytest.mark.asyncio
    async def test_jit_sync_returns_cached_groups(self):
        a = IamAdapter(domain="example.com")
        await a.sync_users([{"id": "kim@example.com", "email": "kim@example.com", "groups": ["eng", "finance"]}])
        out = await a.jit_sync_user_groups("kim@example.com")
        assert "eng" in out["groups"]
        assert "finance" in out["groups"]
        assert out["security_domain"] in ("development", "finance", "general")
    @pytest.mark.asyncio
    async def test_jit_sync_security_domain_after_sync(self):
        a = IamAdapter(domain="example.com")
        await a.sync_users([{"id": "u1", "email": "a@example.com", "groups": ["finance-team"]}])
        out = await a.jit_sync_user_groups("a@example.com")
        assert out["security_domain"] == "finance"
    @pytest.mark.asyncio
    async def test_sync_user_groups_direct(self):
        a = IamAdapter(domain="example.com")
        out = await a.sync_user_groups("newuser@example.com", ["eng", "g2"])
        assert "eng" in out["groups"]
        assert "newuser@example.com" in a._groups["eng"]
class TestDeprovisionRevoke:
    @pytest.mark.asyncio
    async def test_deprovision_revokes_delegations(self):
        ds = DelegationService()
        ledger = AuditLedger(signing_key="test-key")
        a = IamAdapter(domain="example.com", tenant_id="t1", delegation_service=ds, audit_ledger=ledger)
        d1 = ds.grant(user_id="employee:kim", agent_id="agent:assistant:kim", provider="google", scope="gmail.read")
        d2 = ds.grant(user_id="employee:kim", agent_id="agent:assistant:kim", provider="google", scope="calendar.read")
        ds.bind_credential(d1.id, provider="google", secret_ref="secret1", scope="gmail.read")
        d_other = ds.grant(user_id="employee:lee", agent_id="agent:assistant:lee", provider="google", scope="gmail.read")
        await a.sync_users([{"id": "kim@example.com", "email": "kim@example.com", "groups": ["eng"]}])
        await a.sync_groups({"eng": ["kim@example.com", "lee@example.com"]})
        out = await a.deprovision_user("kim@example.com", delegation_service=ds, audit_ledger=ledger)
        assert out["principal"] == "employee:kim"
        assert out["revoked_count"] == 2
        assert d1.id in out["revoked_delegations"]
        assert d2.id in out["revoked_delegations"]
        assert ds.get(d1.id).status.value == "REVOKED"
        assert ds.get(d2.id).status.value == "REVOKED"
        assert ds.is_active(d_other.id) is True
        assert "kim@example.com" not in a._groups["eng"]
        assert ledger.count >= 2
        assert out["audit_events"] >= 2
        assert ledger.verify_chain() is True
    @pytest.mark.asyncio
    async def test_deprovision_idempotent(self):
        ds = DelegationService()
        ledger = AuditLedger(signing_key="k2")
        a = IamAdapter(domain="example.com", tenant_id="t1", delegation_service=ds, audit_ledger=ledger)
        d = ds.grant(user_id="employee:park", agent_id="agent:assistant:park", provider="google", scope="drive.read")
        await a.sync_users([{"id": "park@example.com", "email": "park@example.com"}])
        out1 = await a.deprovision_user("park@example.com", delegation_service=ds, audit_ledger=ledger)
        assert out1["revoked_count"] == 1
        out2 = await a.deprovision_user("park@example.com", delegation_service=ds, audit_ledger=ledger)
        assert out2["revoked_count"] == 0
    @pytest.mark.asyncio
    async def test_deprovision_audit_chain(self):
        ds = DelegationService()
        ledger = AuditLedger(signing_key="audit-secret")
        a = IamAdapter(domain="example.com", tenant_id="cotenant", delegation_service=ds, audit_ledger=ledger)
        d = ds.grant(user_id="employee:audituser", agent_id="agent:assistant:audituser", provider="google", scope="gmail.read")
        await a.sync_users([{"id": "audituser@example.com", "email": "audituser@example.com"}])
        await a.deprovision_user("audituser@example.com")
        cp = ledger.checkpoint()
        assert ledger.verify_checkpoint(cp) is True
        if ledger.events:
            ledger.tamper_event(0, event_type="FAKE")
            assert ledger.verify_chain() is False
    @pytest.mark.asyncio
    async def test_deprovision_cleans_principal_map(self):
        a = IamAdapter(domain="example.com")
        a.register_principal("ext-kim", "employee:kim")
        await a.sync_users([{"id": "kim@example.com", "email": "kim@example.com"}])
        await a.deprovision_user("kim@example.com")
        assert "ext-kim" not in a._principal_map
        assert "kim@example.com" not in a._users
    @pytest.mark.asyncio
    async def test_deprovision_without_services(self):
        a = IamAdapter(domain="example.com")
        await a.sync_users([{"id": "solo@example.com", "email": "solo@example.com", "groups": ["g1"]}])
        await a.sync_groups({"g1": ["solo@example.com"]})
        out = await a.deprovision_user("solo@example.com")
        assert out["removed_users"] >= 1
        assert "g1" in out["groups_removed_from"]
class TestMcpRegistry:
    @pytest.mark.asyncio
    async def test_list_tools_and_resources(self):
        a = IamAdapter()
        tools = await a.list_tools()
        assert "iam_get_user" in tools
        assert "iam_deprovision_user" in tools
        resources = await a.list_resources()
        assert "iam/user/*" in resources
    @pytest.mark.asyncio
    async def test_call_tool_resolve_principal(self):
        a = IamAdapter(domain="example.com", provider="google")
        out = await a.call_tool("iam_resolve_principal", {"email": "test@example.com"}, {})
        assert out["employee_principal"] == "employee:test"
    @pytest.mark.asyncio
    async def test_call_tool_deprovision(self):
        a = IamAdapter(domain="example.com")
        await a.sync_users([{"id": "todep@example.com", "email": "todep@example.com"}])
        out = await a.call_tool("iam_deprovision_user", {"user_id": "todep@example.com"}, {})
        assert out["principal"] == "employee:todep"
    @pytest.mark.asyncio
    async def test_call_tool_unknown_raises(self):
        a = IamAdapter()
        with pytest.raises(ValueError):
            await a.call_tool("unknown_tool", {}, {})
    def test_required_scope_and_action(self):
        a = IamAdapter()
        assert a.required_scope("iam_get_user") == "directory.read"
        assert a.tool_action("iam_get_user") == "READ"
        assert a.tool_action("unknown") == "READ"
