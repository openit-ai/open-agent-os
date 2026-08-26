"""MVP Demo — Phase 1 Core Personal Agent (Section 3.1 morning briefing).

5 tests — spec requires 86 + 5 = 91:
  1. kim 브리핑 성공 (09:30 고객미팅 / 11:00 개발회의 / 오늘 반드시 처리 + mattermost keyword parity)
  2. trace 유지 (trace_id maintained across orchestrator + audit + response)
  3. Audit 체인 검증 (hash-chain valid, 14+ events)
  4. cross-user isolation (kim 데이터가 lee에게 노출 안 됨)
  5. Explicit Deny (export) 차단 — personal Delegation override
"""
import pytest
from fastapi.testclient import TestClient

from control_plane.app import app as cp_app
from execution_gateway.normalize import normalize_resource, canonicalize_action
from execution_gateway.authz_hook import AuthorizationHook
from execution_gateway.mock_executor import get_ledger, reset_ledger


@pytest.fixture(autouse=True)
def isolate_ledger():
    reset_ledger()
    yield


client = TestClient(cp_app)


def test_kim_briefing_success():
    """kim 브리핑 성공 — Section 3.1 format + Mattermost keyword parity."""
    r = client.post("/v1/demo/morning-briefing", json={}, headers={"X-User-Id": "employee:kim"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "trace_id" in data and data["trace_id"].startswith("trace_")
    assert "briefing" in data
    briefing = data["briefing"]
    sections = briefing.get("sections", {})
    assert "09:30 고객 A 미팅 준비" in sections, f"missing 09:30, got {list(sections.keys())}"
    assert "11:00 개발회의" in sections
    assert "오늘 반드시 처리" in sections
    counts = briefing.get("counts", {})
    assert counts.get("calendar_today", 0) >= 2
    assert "업무 브리핑" in briefing.get("summary_text", "")
    sources = data.get("sources", {})
    for key in ["calendar", "gmail", "tasks", "drive", "outline", "mattermost", "crm"]:
        assert key in sources, f"missing source {key}"
        assert sources[key].get("status") == "ok", f"source {key} not ok: {sources[key]}"
        assert sources[key].get("trace_id") == data["trace_id"]
    assert data.get("tenant_id") is not None

    # Mattermost parity — "정리해줘" keyword routes to same briefing via webhook
    payload = {"tenant_id": "default", "user_id": "employee:kim", "text": "오늘 내가 처리해야 할 업무 정리해줘"}
    r2 = client.post("/v1/mattermost/events", json=payload)
    assert r2.status_code == 200, r2.text
    data2 = r2.json()
    assert data2.get("routed") == "morning-briefing"
    assert "briefing" in data2
    assert "09:30 고객 A 미팅 준비" in data2["briefing"].get("sections", {})


def test_trace_propagated():
    """trace 유지 — all source trace_ids must match top-level trace_id."""
    headers_kim = {"X-User-Id": "employee:kim", "X-Tenant-Id": "test-tenant"}
    r = client.post("/v1/demo/morning-briefing", json={"tenant_id": "test-tenant"}, headers=headers_kim)
    assert r.status_code == 200, r.text
    data = r.json()
    trace_id = data["trace_id"]
    for src, payload in data.get("sources", {}).items():
        assert payload.get("trace_id") == trace_id, f"source {src} trace mismatch"
    # Audit events also carry same trace_id
    ledger = get_ledger()
    assert ledger is not None
    for ev in ledger.events:
        assert ev.trace_id == trace_id, f"event {ev.event_id} trace mismatch {ev.trace_id} != {trace_id}"


def test_audit_chain_verified():
    """Audit 체인 검증 — hash-chain valid, checkpoint sign/verify."""
    r = client.post("/v1/demo/morning-briefing", json={}, headers={"X-User-Id": "employee:kim"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("audit") is not None
    assert data["audit"].get("chain_valid") is True
    ledger = get_ledger()
    assert ledger is not None
    assert ledger.count >= 14, f"expected >=14 audit events (7 tools x2), got {ledger.count}"
    assert ledger.verify_chain() is True, "audit hash chain broken"
    cp = ledger.checkpoint()
    assert ledger.verify_checkpoint(cp) is True
    # Tamper detection — modify resource to break hash chain
    ledger.tamper_event(0, resource="tampered/resource")
    assert ledger.verify_chain() is False


def test_cross_user_isolation():
    """cross-user isolation — kim 데이터가 lee에게 노출 안 됨."""
    r_kim = client.post("/v1/demo/morning-briefing", json={}, headers={"X-User-Id": "employee:kim"})
    assert r_kim.status_code == 200
    kim_data = r_kim.json()
    reset_ledger()
    r_lee = client.post("/v1/demo/morning-briefing", json={}, headers={"X-User-Id": "employee:lee"})
    assert r_lee.status_code == 200
    lee_data = r_lee.json()

    kim_cal_titles = [c.get("title", "") for c in kim_data["sources"]["calendar"]["items"]]
    lee_cal_titles = [c.get("title", "") for c in lee_data["sources"]["calendar"]["items"]]
    assert any("고객 A" in t for t in kim_cal_titles), f"kim missing 고객 A, got {kim_cal_titles}"
    assert not any("고객 A" in t for t in lee_cal_titles), f"lee leaked kim data: {lee_cal_titles}"

    kim_gmail_subjects = [m.get("subject", "") for m in kim_data["sources"]["gmail"]["items"]]
    lee_gmail_subjects = [m.get("subject", "") for m in lee_data["sources"]["gmail"]["items"]]
    assert any("A사 제안서" in s for s in kim_gmail_subjects)
    assert not any("A사 제안서" in s for s in lee_gmail_subjects)
    assert kim_data["sources"]["mattermost"]["items"] != lee_data["sources"]["mattermost"]["items"]

    # Direct authz: lee cannot read kim's gmail
    import asyncio
    async def _check():
        hook = AuthorizationHook(tenant_id="default")
        ctx_lee = {"user_id": "employee:lee", "agent_id": "agent:assistant:lee", "tenant_id": "default", "trace_id": "trace_isolation_test"}
        return await hook.authorize(ctx_lee, action="SEARCH", resource="gmail/user/kim/messages", tool_name="gmail.search")
    res = asyncio.run(_check())
    assert res.allowed is False
    assert res.decision == "DENY"
    # HTTP gateway cross-user
    from execution_gateway.app import app as gw_app
    gw_client = TestClient(gw_app)
    ctx = {"user_id": "employee:lee", "agent_id": "agent:assistant:lee", "tenant_id": "default", "trace_id": "trace_iso_http", "request_id": "req_iso"}
    import json
    r = gw_client.post("/v1/execute", json={"tool": "gmail_search", "action": "SEARCH", "resource": "gmail/user/kim/messages", "args": {}}, headers={"X-Agent-Context": json.dumps(ctx)})
    assert r.status_code == 403


def test_explicit_deny_export_blocked():
    """Explicit Deny (export) 차단 — personal Delegation을 override (Section 25)."""
    import asyncio
    from policy_model.model import PolicyBundle, PolicyRule, PolicySource, PolicyDecision
    from policy_engine.engine import PolicyEngine

    bundle_deny = PolicyBundle(
        id="test-deny-export",
        tenant_id="default",
        name="test",
        version="1.0",
        rules=[
            PolicyRule(id="deny-export-all", source=PolicySource.EXPLICIT_DENY, action="EXPORT", resource_pattern="*", effect=PolicyDecision.DENY),
            PolicyRule(id="allow-personal-read", source=PolicySource.PERSONAL_DELEGATION, action="READ", resource_pattern="gmail/user/*", effect=PolicyDecision.ALLOW),
        ],
    )
    engine = PolicyEngine([bundle_deny])
    hook = AuthorizationHook(policy_engine=engine, tenant_id="default")
    ctx_kim = {"user_id": "employee:kim", "agent_id": "agent:assistant:kim", "tenant_id": "default", "trace_id": "trace_export_test", "session_id": "sess_1", "request_id": "req_1"}

    async def _check_export():
        return await hook.authorize(ctx_kim, action="EXPORT", resource="gmail/user/kim/messages", tool_name="gmail.export")
    res = asyncio.run(_check_export())
    assert res.allowed is False
    assert res.decision == "DENY"
    assert res.source == "explicit_deny"

    # Demo's export_check field must also be DENY (orchestrator engine denies EXPORT)
    r = client.post("/v1/demo/morning-briefing", json={}, headers={"X-User-Id": "employee:kim"})
    assert r.status_code == 200
    export_check = r.json().get("export_check", {})
    assert export_check.get("decision") == "DENY", f"expected export DENY, got {export_check}"
    assert export_check.get("action") == "EXPORT"
