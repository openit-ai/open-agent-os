"""Tests — §16G/§16I Data Access Pattern + Blast Radius (7 tests)

- read path 2
- write path 2
- direct DB DENY 2
- blast radius 경계 1
"""
from __future__ import annotations

import pytest

from execution_gateway.data_access import (
    DataAccessPolicy,
    allowed_read_sources,
    allowed_write_sources,
    ALLOWED_READ_SOURCES,
    ALLOWED_WRITE_SOURCES,
)

@pytest.fixture
def policy():
    return DataAccessPolicy()

# ── read path 2 ────────────────────────────────────────────────────

def test_read_path_read_requires_read_only_api(policy):
    """READ → require read_only_api (16I.1)"""
    r = policy.read_path("READ", "outline/team/docs")
    assert r.allowed is True
    assert r.decision == "ALLOW"
    assert r.required_source == "read_only_api"
    assert r.requires_approval is False
    assert "read_only_api" in allowed_read_sources
    assert r.reason

def test_read_path_search_requires_read_only_api(policy):
    """SEARCH → require read_only_api (16I.1) — least data/field/row"""
    r = policy.read_path("SEARCH", "crm/customer/acme")
    assert r.allowed is True
    assert r.decision == "ALLOW"
    assert r.required_source == "read_only_api"
    # also via generic check
    c = policy.check("SEARCH", "outline/team/docs")
    assert c.decision == "ALLOW"
    assert c.required_source == "read_only_api"

# ── write path 2 ───────────────────────────────────────────────────

def test_write_path_create_requires_command_api_and_approval(policy):
    """CREATE → require command_api + approval (16I.2)"""
    r = policy.write_path("CREATE", "outline/team/docs")
    assert r.decision == "APPROVAL_REQUIRED"
    assert r.required_source == "command_api"
    assert r.requires_approval is True
    assert "command_api" in allowed_write_sources
    assert r.allowed is False  # not directly allowed without approval

def test_write_path_delete_and_pay_require_approval(policy):
    """DELETE/DEPLOY/MERGE/PAY → command_api + approval"""
    for action in ["DELETE", "DEPLOY", "MERGE", "PAY", "MODIFY"]:
        r = policy.write_path(action, f"production/orders/{action.lower()}")
        assert r.decision == "APPROVAL_REQUIRED", f"{action} should be APPROVAL_REQUIRED"
        assert r.required_source == "command_api"
        assert r.requires_approval is True

# ── direct DB DENY 2 ───────────────────────────────────────────────

def test_direct_db_access_always_deny_hermes(policy):
    """Hermes → Production DB = DENY (16I.3)"""
    r = policy.direct_db_access("hermes", "production/db/customers")
    assert r.allowed is False
    assert r.decision == "DENY"
    assert "DENY" in r.reason
    # any user
    r2 = policy.direct_db_access("employee:kim", "production/db/orders")
    assert r2.decision == "DENY"
    # via generic check with direct_db source
    c = policy.check("READ", "production/db/customers", source="direct_db", user="hermes")
    assert c.decision == "DENY"
    assert c.allowed is False

def test_direct_db_access_denied_via_resource_keyword(policy):
    """resource에 production 포함 시 DENY — 모든 user"""
    r = policy.check("READ", "production/prod_db/secret", user="hermes")
    assert r.decision == "DENY"
    r2 = policy.check("SEARCH", "production/customer_table", source="direct_db", user="employee:lee")
    assert r2.decision == "DENY"
    # allowed_read_sources / write_sources 정의 확인
    assert "read_only_api" in ALLOWED_READ_SOURCES
    assert "command_api" in ALLOWED_WRITE_SOURCES
    assert "mcp" in ALLOWED_READ_SOURCES
    assert "mcp" in ALLOWED_WRITE_SOURCES

# ── blast radius 경계 1 ────────────────────────────────────────────

def test_blast_radius_boundary_hermes_cannot_access_production(policy):
    """§16G Blast Radius — Hermes compromised 시에도 Production 자원 접근 불가"""
    # Hermes는 /home/hermes 외 production/erp/crm/vault 접근 불가
    denied_resources = [
        "production/db/customers",
        "erp/orders/123",
        "crm/customer/acme",
        "credential_vault/secret/api_key",
        "security_core/secret",
    ]
    for res in denied_resources:
        r = policy.check_blast_radius("hermes", res, action="READ")
        assert r.decision == "DENY", f"{res} should be DENY in blast radius"
        assert r.allowed is False

    # 허용 경계: /home/hermes 내 작업은 허용
    allowed = policy.check_blast_radius("hermes", "/home/hermes/workspace/task.py", action="EXECUTE")
    assert allowed.decision == "ALLOW"

@pytest.mark.asyncio
async def test_proxy_hook_blocks_direct_db_access():
    """proxy_tool_call에서 data_access.check() 훅이 direct DB를 차단 (stub 결정론)"""
    from execution_gateway.proxy import proxy_tool_call

    # direct production resource → DATA_ACCESS_DENIED
    result = await proxy_tool_call(
        tool_name="crm_search",
        args={"query": "test"},
        capability_token=None,
        context={
            "action": "READ",
            "resource": "production/db/customers",
            "user_id": "hermes",
            "trace_id": "trace_test123",
            "request_id": "req_test123",
            "data_source": "direct_db",
        },
    )
    assert result.get("error") == "DATA_ACCESS_DENIED"
    assert "DENY" in result.get("reason", "") or "DENY" in str(result.get("data_access", ""))

    # 정상 read 경로는 차단되지 않음 (mock/stub 성공)
    result2 = await proxy_tool_call(
        tool_name="outline_search",
        args={"query": "test"},
        capability_token=None,
        context={
            "action": "SEARCH",
            "resource": "outline/team/docs",
            "user_id": "employee:kim",
            "trace_id": "trace_test123",
            "request_id": "req_test123",
        },
    )
    assert result2.get("error") != "DATA_ACCESS_DENIED"
    assert result2.get("ok") is True
