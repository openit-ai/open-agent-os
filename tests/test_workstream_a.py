"""Workstream A — Section 37 완료조건 검증.

- 1 user = 1 logical agent (deterministic)
- cross-user session isolation
- stream response 정상
- user context 유지 (AgentContext = tenant/user/agent/session/trace)
"""
import pytest
from control_plane.identity import map_user_to_agent
from control_plane.personal_agent import derive_agent_id
from control_plane.session import SessionStore
from control_plane.router import select_worker_pool

def test_one_user_one_agent_deterministic():
    m1 = map_user_to_agent("employee:kim", "tenant-a")
    m2 = map_user_to_agent("employee:kim", "tenant-a")
    assert m1.agent_principal == m2.agent_principal == "agent:assistant:kim"
    assert derive_agent_id("employee:kim") == "agent:assistant:kim"
    assert derive_agent_id("employee:lee") == "agent:assistant:lee"

def test_different_users_different_agents():
    kim = map_user_to_agent("employee:kim", "t")
    lee = map_user_to_agent("employee:lee", "t")
    assert kim.agent_principal != lee.agent_principal

def test_cross_user_session_isolation():
    store = SessionStore()
    rec = store.create("t", "employee:kim", "agent:assistant:kim")
    # owner can read
    assert store.get(rec.session_id, "employee:kim").session_id == rec.session_id
    # other user denied
    with pytest.raises(PermissionError):
        store.get(rec.session_id, "employee:lee")
    # owner check on prompt
    with pytest.raises(PermissionError):
        store.append_prompt(rec.session_id, "employee:lee", "hack", "req_1")

def test_session_context_preserved():
    store = SessionStore()
    rec = store.create("customer", "employee:kim", "agent:assistant:kim", security_domain="development")
    ctx = rec.to_agent_context(request_id="req_test")
    assert ctx["tenant_id"] == "customer"
    assert ctx["user_id"] == "employee:kim"
    assert ctx["agent_id"] == "agent:assistant:kim"
    assert ctx["session_id"] == rec.session_id
    assert ctx["trace_id"] == rec.trace_id
    assert ctx["request_id"] == "req_test"
    assert ctx["security_domain"] == "development"

def test_worker_pool_routing():
    assert select_worker_pool("general") == "hermes-general"
    assert select_worker_pool("development") == "hermes-dev"
    assert select_worker_pool("finance_hr") == "hermes-finance-hr"
    # high-risk → ephemeral
    assert select_worker_pool("general", risk_level="HIGH") == "hermes-ephemeral"
    assert select_worker_pool("general", action="DEPLOY") == "hermes-ephemeral"
    assert select_worker_pool("general", action="MERGE") == "hermes-ephemeral"

def test_stream_buffer():
    store = SessionStore()
    rec = store.create("t", "employee:kim", "agent:assistant:kim")
    store.append_stream_event(rec.session_id, {"type": "token", "data": {"text": "hi"}})
    assert len(store.get(rec.session_id, "employee:kim").stream_events) == 1

def test_identity_requires_namespace():
    with pytest.raises(ValueError):
        map_user_to_agent("kim", "t")
    with pytest.raises(ValueError):
        map_user_to_agent("", "t")
