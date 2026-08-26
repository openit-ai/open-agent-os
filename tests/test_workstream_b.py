"""Workstream B — Execution Gateway 완성 검증 (Sections 7.2, 19-21, 37-38)

완료조건:
- personal credential owner 검증
- unauthorized enterprise action deny
- capability validation (HIGH-risk는 token 필수 + binding)
- trace 유지

Coverage:
  normalize / mcp_registry / google connector / outline connector / risk / authz_hook / proxy / app
"""
import pytest
import json
import base64
from datetime import datetime, timedelta, timezone

# ── normalize ────────────────────────────────────────────────────────
from execution_gateway.normalize import (
    canonicalize_action,
    normalize_resource,
    parse_resource,
    is_personal_resource,
    extract_owner_user_id,
)
from execution_gateway.mcp_registry import MCPRegistry, MCPServer, default_registry
from execution_gateway.risk import classify, RiskLevel
from execution_gateway.connectors.google import GoogleConnector
from execution_gateway.connectors.outline import OutlineConnector
from execution_gateway.capability import verify_capability
from execution_gateway.authz_hook import AuthorizationHook
from execution_gateway.proxy import proxy_tool_call


# ── helpers ──────────────────────────────────────────────────────────
def make_ctx(user="employee:kim", tenant="test-tenant", agent=None, trace="trace_test_123", delegation=None, credential=None):
    agent = agent or user.replace("employee:", "agent:assistant:", 1)
    return {
        "user_id": user,
        "agent_id": agent,
        "tenant_id": tenant,
        "session_id": "sess_test",
        "trace_id": trace,
        "request_id": "req_test",
        "delegation_id": delegation,
        "credential_binding_id": credential,
    }


# ── normalize tests ──────────────────────────────────────────────────

def test_canonicalize_action():
    assert canonicalize_action("read") == "READ"
    assert canonicalize_action("SEND_EMAIL") == "SEND"
    assert canonicalize_action("get") == "READ"
    assert canonicalize_action("search") == "SEARCH"
    with pytest.raises(ValueError):
        canonicalize_action("unknown_action_xyz")


def test_normalize_resource_canonical():
    assert normalize_resource("gmail/user/kim/messages") == "gmail/user/kim/messages"
    assert normalize_resource("gmail://user/kim/messages") == "gmail/user/kim/messages"
    assert normalize_resource("/gmail/user/kim/") == "gmail/user/kim"
    assert normalize_resource("GMAIL/user/kim") == "gmail/user/kim"


def test_parse_resource_personal():
    p = parse_resource("gmail/user/kim/messages/123")
    assert p.domain == "gmail"
    assert p.scope == "user/kim"
    assert p.path == "messages/123"
    assert p.is_personal is True
    assert is_personal_resource("gmail/user/kim/*") is True
    assert is_personal_resource("outline/team/docs") is False
    assert extract_owner_user_id("gmail/user/kim/messages") == "employee:kim"
    assert extract_owner_user_id("outline/team/docs") is None


def test_normalize_invalid():
    with pytest.raises(ValueError):
        normalize_resource("")
    with pytest.raises(ValueError):
        canonicalize_action("")


# ── MCP Registry ─────────────────────────────────────────────────────

def test_mcp_registry_register_and_discovery():
    reg = MCPRegistry()
    reg.register(MCPServer(name="google", transport="streamable-http", tools=["gmail_search"], resources=["gmail/user/*"]))
    assert "gmail_search" in reg.list_tools()
    srv = reg.find_tool("gmail_search")
    assert srv is not None and srv.name == "google"
    assert reg.find_tool("nonexistent") is None
    # normalization & wildcard matching
    srv2 = reg.find_resource("gmail/user/kim/messages/123")
    assert srv2 is not None
    # detailed
    assert len(reg.list_tools_detailed()) == 1
    # unregister
    assert reg.unregister("google") is True
    assert reg.find_tool("gmail_search") is None
    assert reg.unregister("ghost") is False


def test_default_registry_has_google_and_outline():
    assert "gmail_search" in default_registry.list_tools()
    assert "outline_search" in default_registry.list_tools()
    assert default_registry.find_tool("gmail_send") is not None
    assert default_registry.find_tool("outline_read") is not None


def test_mcp_registry_normalize_tool_call():
    reg = MCPRegistry()
    norm = reg.normalize_tool_call("gmail_read", "gmail://user/kim/messages", "get")
    assert norm["resource"] == "gmail/user/kim/messages"
    assert norm["action"] == "READ"


# ── Risk classification ──────────────────────────────────────────────

def test_risk_low():
    assert classify("SEARCH", "outline/team/docs") == RiskLevel.MEDIUM  # outline read is MEDIUM per spec
    assert classify("READ", "public/web", data_classification="PUBLIC") == RiskLevel.LOW
    assert classify("SEARCH", "public/web") == RiskLevel.LOW


def test_risk_medium():
    assert classify("CREATE", "calendar/user/kim/events") == RiskLevel.MEDIUM
    assert classify("MODIFY", "tasks/user/kim/items") == RiskLevel.MEDIUM
    assert classify("READ", "outline/team/docs") == RiskLevel.MEDIUM


def test_risk_high_actions():
    for action in ["SEND", "DELETE", "DEPLOY", "MERGE", "PAY", "EXPORT", "SHARE", "ADMIN"]:
        assert classify(action, "gmail/user/kim/messages") == RiskLevel.HIGH


def test_risk_high_external_bulk_pii():
    assert classify("READ", "drive/user/kim/files", is_external=True) == RiskLevel.HIGH
    assert classify("READ", "drive/user/kim/bulk/export") == RiskLevel.HIGH
    assert classify("READ", "drive/user/kim/files", arg_hints={"bulk": True}) == RiskLevel.HIGH
    assert classify("READ", "crm/pii/records") == RiskLevel.HIGH
    assert classify("SEND", "gmail/user/kim/messages", arg_hints={"recipient": "external@evil.com"}) == RiskLevel.HIGH
    # PII classification + keyword → HIGH
    assert classify("READ", "crm/pii/export", data_classification="PII") == RiskLevel.HIGH


# ── Google Connector owner isolation ─────────────────────────────────

def test_google_owner_verified():
    gc = GoogleConnector()
    ctx = make_ctx(user="employee:kim")
    ok = gc.check_owner(ctx, "gmail/user/kim/messages/123")
    assert ok.allowed is True

    # cross-user — DENY
    ctx_lee = make_ctx(user="employee:lee")
    deny = gc.check_owner(ctx_lee, "gmail/user/kim/messages/123")
    assert deny.allowed is False
    assert "owner mismatch" in deny.reason


def test_google_non_personal_passthrough():
    gc = GoogleConnector()
    ctx = make_ctx(user="employee:kim")
    # enterprise resource는 google isolation 대상 아님 → allow
    ok = gc.check_owner(ctx, "outline/team/docs")
    assert ok.allowed is True

    # different domain personal (not google) → passthrough
    ok2 = gc.check_owner(ctx, "notion/user/kim/page")
    assert ok2.allowed is True


def test_google_missing_user():
    gc = GoogleConnector()
    deny = gc.check_owner({}, "gmail/user/kim/messages")
    assert deny.allowed is False


# ── Outline ACL ──────────────────────────────────────────────────────

def test_outline_acl_pass():
    oc = OutlineConnector()
    ctx = make_ctx(tenant="tenant-a")
    res = oc.check_acl(ctx, "outline/team/docs", action="READ")
    assert res.allowed is True


def test_outline_non_outline_passthrough():
    oc = OutlineConnector()
    ctx = make_ctx()
    res = oc.check_acl(ctx, "gmail/user/kim/messages")
    assert res.allowed is True


# ── Capability verification ──────────────────────────────────────────

def test_capability_ok_and_mismatch():
    from jose import jwt
    key = "test-secret-key-for-gateway-tests"
    payload = {
        "sub": "agent:assistant:kim",
        "on_behalf_of": "employee:kim",
        "action": "READ",
        "resource": "gmail/user/kim/*",
        "session_id": "sess_test",
        "request_id": "req_test",
        "jti": "jti_123",
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
    }
    token_str = jwt.encode(payload, key, algorithm="HS256")
    decoded = jwt.decode(token_str, key, algorithms=["HS256"])

    ctx = make_ctx()
    ok = verify_capability(decoded, "READ", "gmail/user/kim/messages/123", ctx)
    assert ok.allowed is True

    # action mismatch
    bad = verify_capability(decoded, "SEND", "gmail/user/kim/messages/123", ctx)
    assert bad.allowed is False

    # resource mismatch
    bad2 = verify_capability(decoded, "READ", "outline/team/docs", ctx)
    assert bad2.allowed is False

    # expired token
    payload_exp = {**payload, "exp": int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp())}
    token_exp = jwt.encode(payload_exp, key, algorithm="HS256")
    decoded_exp = jwt.decode(token_exp, key, algorithms=["HS256"], options={"verify_exp": False})
    expired_check = verify_capability(decoded_exp, "READ", "gmail/user/kim/messages", ctx)
    assert expired_check.allowed is False


def test_capability_delegation_binding():
    ctx = make_ctx(delegation="dlg_abc")
    token = {"action": "READ", "resource": "gmail/user/kim/*", "jti": "j1", "delegation_id": "dlg_abc", "on_behalf_of": "employee:kim"}
    ok = verify_capability(token, "READ", "gmail/user/kim/messages", ctx)
    assert ok.allowed is True

    # mismatch delegation
    token2 = {**token, "delegation_id": "dlg_other"}
    bad = verify_capability(token2, "READ", "gmail/user/kim/messages", ctx)
    assert bad.allowed is False


# ── Authorization Hook ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_authz_personal_owner_enforced():
    hook = AuthorizationHook(tenant_id="test-tenant")
    ctx = make_ctx(user="employee:kim")
    # own resource → ALLOW (personal delegation)
    res = await hook.authorize(ctx, action="READ", resource="gmail/user/kim/messages")
    assert res.allowed is True
    assert res.decision == "ALLOW"

    # cross-user personal → DENY (owner isolation)
    # lee가 kim의 mailbox에 접근 시도
    ctx_lee = make_ctx(user="employee:lee")
    res2 = await hook.authorize(ctx_lee, action="READ", resource="gmail/user/kim/messages")
    assert res2.allowed is False
    assert res2.decision == "DENY"
    assert "owner" in res2.reason.lower()


@pytest.mark.asyncio
async def test_authz_enterprise_deny_and_approval():
    hook = AuthorizationHook(tenant_id="test-tenant")
    ctx = make_ctx()
    # enterprise MERGE는 APPROVAL_REQUIRED (Section 12, default bundle)
    res = await hook.authorize(ctx, action="MERGE", resource="github/openit-ai/app/pr/1")
    assert res.allowed is False
    assert res.decision in ("APPROVAL_REQUIRED", "DENY")

    # enterprise DEPLOY도 HIGH — approval or deny
    res2 = await hook.authorize(ctx, action="DEPLOY", resource="production/service/api")
    assert res2.allowed is False


@pytest.mark.asyncio
async def test_authz_explicit_deny_overrides_personal():
    """Explicit Deny가 Personal Delegation을 override (Section 25)."""
    from policy_model.model import PolicyBundle, PolicyRule, PolicySource, PolicyDecision
    from policy_engine.engine import PolicyEngine

    bundle = PolicyBundle(
        id="test-bundle", tenant_id="test-tenant", name="test", version="1.0",
        rules=[
            PolicyRule(id="deny-gmail-export", source=PolicySource.EXPLICIT_DENY, action="EXPORT", resource_pattern="gmail/*", effect=PolicyDecision.DENY),
            PolicyRule(id="allow-personal-read", source=PolicySource.PERSONAL_DELEGATION, action="READ", resource_pattern="gmail/user/*", effect=PolicyDecision.ALLOW),
        ],
    )
    engine = PolicyEngine([bundle])
    hook = AuthorizationHook(policy_engine=engine, tenant_id="test-tenant")
    ctx = make_ctx()

    # EXPORT는 explicit deny → DENY (personal이여도)
    res = await hook.authorize(ctx, action="EXPORT", resource="gmail/user/kim/messages")
    assert res.allowed is False
    assert res.decision == "DENY"


# ── Proxy ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_proxy_low_risk_no_token_ok():
    ctx = make_ctx(trace="trace_low_001")
    res = await proxy_tool_call("gmail_search", {"q": "hello"}, None, {**ctx, "action": "SEARCH", "resource": "gmail/user/kim/messages"})
    assert res.get("ok") is True
    assert res["trace_id"] == "trace_low_001"
    assert res["risk"] in ("LOW", "MEDIUM")  # SEARCH on gmail is MEDIUM or LOW


@pytest.mark.asyncio
async def test_proxy_high_requires_capability():
    ctx = make_ctx(trace="trace_high_002")
    # HIGH-risk SEND without token → CAPABILITY_REQUIRED
    res = await proxy_tool_call("gmail_send", {"to": "external@evil.com"}, None, {**ctx, "action": "SEND", "resource": "gmail/user/kim/messages"})
    assert res.get("error") == "CAPABILITY_REQUIRED"
    assert res["trace_id"] == "trace_high_002"


@pytest.mark.asyncio
async def test_proxy_high_with_valid_token():
    from jose import jwt
    key = "test-secret"
    payload = {
        "sub": "agent:assistant:kim",
        "on_behalf_of": "employee:kim",
        "action": "SEND",
        "resource": "gmail/user/kim/*",
        "session_id": "sess_test",
        "request_id": "req_test",
        "jti": "jti_send_1",
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
    }
    token_str = jwt.encode(payload, key, algorithm="HS256")
    decoded = jwt.decode(token_str, key, algorithms=["HS256"])

    ctx = make_ctx(trace="trace_high_003")
    res = await proxy_tool_call("gmail_send", {"to": "a@b.com"}, decoded, {**ctx, "action": "SEND", "resource": "gmail/user/kim/messages"})
    assert res.get("ok") is True
    assert res["trace_id"] == "trace_high_003"
    assert res["risk"] == "HIGH"


@pytest.mark.asyncio
async def test_proxy_capability_action_mismatch():
    token = {"action": "READ", "resource": "gmail/user/kim/*", "jti": "j1", "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())}
    ctx = make_ctx(trace="trace_mismatch")
    res = await proxy_tool_call("gmail_send", {}, token, {**ctx, "action": "SEND", "resource": "gmail/user/kim/messages"})
    assert res.get("error") == "CAPABILITY_DENIED"


@pytest.mark.asyncio
async def test_proxy_trace_propagated():
    ctx = {"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "tenant_id": "t", "trace_id": "trace_prop_999", "request_id": "req_abc", "session_id": "sess_1", "action": "READ", "resource": "gmail/user/kim/messages"}
    res = await proxy_tool_call("gmail_read", {}, None, ctx)
    assert res["trace_id"] == "trace_prop_999"
    assert res["request_id"] == "req_abc"


# ── FastAPI App (TestClient) ─────────────────────────────────────────

def test_app_health():
    from fastapi.testclient import TestClient
    from execution_gateway.app import app
    c = TestClient(app)
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_app_tools():
    from fastapi.testclient import TestClient
    from execution_gateway.app import app
    c = TestClient(app)
    r = c.get("/v1/tools")
    assert r.status_code == 200
    data = r.json()
    assert "tools" in data
    assert len(data["tools"]) > 0
    assert "servers" in data


def test_app_execute_personal_success():
    from fastapi.testclient import TestClient
    from execution_gateway.app import app
    c = TestClient(app)
    ctx = {"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "tenant_id": "test-tenant", "session_id": "sess_1", "trace_id": "trace_app_001", "request_id": "req_app_001"}
    headers = {"X-Agent-Context": json.dumps(ctx)}
    body = {"tool": "gmail_search", "action": "SEARCH", "resource": "gmail/user/kim/messages", "args": {"q": "hello"}}
    r = c.post("/v1/execute", json=body, headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("ok") is True
    assert data["trace_id"] == "trace_app_001"


def test_app_execute_cross_user_denied():
    from fastapi.testclient import TestClient
    from execution_gateway.app import app
    c = TestClient(app)
    # lee가 kim의 메일 조회 → DENY
    ctx = {"user_id": "employee:lee", "agent_id": "agent:assistant:lee", "tenant_id": "test-tenant", "session_id": "sess_2", "trace_id": "trace_app_002", "request_id": "req_app_002"}
    headers = {"X-Agent-Context": json.dumps(ctx)}
    body = {"tool": "gmail_search", "action": "SEARCH", "resource": "gmail/user/kim/messages", "args": {}}
    r = c.post("/v1/execute", json=body, headers=headers)
    assert r.status_code == 403
    assert r.json()["trace_id"] == "trace_app_002"


def test_app_execute_enterprise_approval_required():
    from fastapi.testclient import TestClient
    from execution_gateway.app import app
    c = TestClient(app)
    ctx = {"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "tenant_id": "test-tenant", "session_id": "sess_3", "trace_id": "trace_app_003", "request_id": "req_app_003"}
    headers = {"X-Agent-Context": json.dumps(ctx)}
    # enterprise MERGE → APPROVAL_REQUIRED or DENY
    # need a tool that exists: use gmail_send but with enterprise resource? better use a known enterprise path
    # default_registry has no github tool, so we use outline_modify with DEPLOY action to trigger HIGH
    body = {"tool": "gmail_send", "action": "MERGE", "resource": "github/openit-ai/app/pr/99", "args": {}}
    # find a tool that exists for this test — gmail_send exists, but resource is github
    r = c.post("/v1/execute", json=body, headers=headers)
    # Should be 403 (APPROVAL_REQUIRED or DENY)
    assert r.status_code == 403
    assert "trace_id" in r.json()


def test_app_execute_high_without_token_denied():
    from fastapi.testclient import TestClient
    from execution_gateway.app import app
    c = TestClient(app)
    ctx = {"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "tenant_id": "test-tenant", "session_id": "sess_4", "trace_id": "trace_app_004", "request_id": "req_app_004"}
    headers = {"X-Agent-Context": json.dumps(ctx)}
    body = {"tool": "gmail_send", "action": "SEND", "resource": "gmail/user/kim/messages", "args": {"to": "x@y.com"}}
    r = c.post("/v1/execute", json=body, headers=headers)
    assert r.status_code == 403
    data = r.json()
    assert data["error"] in ("CAPABILITY_REQUIRED", "CAPABILITY_DENIED", "DENIED", "APPROVAL_REQUIRED")
    # trace 유지
    assert data.get("trace_id") == "trace_app_004"


def test_app_execute_missing_context_unauthorized():
    from fastapi.testclient import TestClient
    from execution_gateway.app import app
    c = TestClient(app)
    body = {"tool": "gmail_search", "action": "SEARCH", "resource": "gmail/user/kim/messages", "args": {}}
    r = c.post("/v1/execute", json=body)
    assert r.status_code in (400, 401)


def test_app_execute_base64_context():
    from fastapi.testclient import TestClient
    from execution_gateway.app import app
    c = TestClient(app)
    ctx = {"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "tenant_id": "test-tenant", "session_id": "sess_5", "trace_id": "trace_b64_001", "request_id": "req_b64_001"}
    b64 = base64.b64encode(json.dumps(ctx).encode()).decode()
    headers = {"X-Agent-Context": b64}
    body = {"tool": "gmail_search", "action": "SEARCH", "resource": "gmail/user/kim/messages", "args": {}}
    r = c.post("/v1/execute", json=body, headers=headers)
    assert r.status_code == 200


def test_app_trace_header_propagated():
    from fastapi.testclient import TestClient
    from execution_gateway.app import app
    c = TestClient(app)
    ctx = {"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "tenant_id": "test-tenant", "session_id": "sess_6", "trace_id": "trace_hdr_777", "request_id": "req_hdr_777"}
    headers = {"X-Agent-Context": json.dumps(ctx)}
    body = {"tool": "gmail_read", "action": "READ", "resource": "gmail/user/kim/messages/1", "args": {}}
    r = c.post("/v1/execute", json=body, headers=headers)
    assert r.status_code == 200
    assert r.headers.get("x-trace-id") == "trace_hdr_777" or r.json().get("trace_id") == "trace_hdr_777"
