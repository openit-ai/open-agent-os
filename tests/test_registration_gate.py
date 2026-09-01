from __future__ import annotations

import os


def test_registration_gate_requires_admin_mapping(monkeypatch):
    from control_plane import registration_gate

    monkeypatch.setattr(registration_gate, "_MAPPING_PROVIDER", lambda tenant, user: None, raising=False)
    result = registration_gate.handle(
        tenant_id="t1", user_id="employee:lee", session_id="s1",
        text="hello", platform="mattermost",
    )
    assert result.allowed is False
    assert result.state == "UNREGISTERED"
    assert "관리자" in result.response


def test_registration_gate_progresses_to_session_ok(monkeypatch):
    from control_plane import registration_gate

    monkeypatch.delenv("OAOS_ENV", raising=False)
    monkeypatch.setattr(registration_gate, "_MAPPING_PROVIDER", lambda tenant, user: {
        "employee_principal": user,
        "agent_id": "agent:assistant:kim",
        "status": "active",
    }, raising=False)
    registration_gate._memory.clear()
    kwargs = dict(tenant_id="t1", user_id="employee:kim", session_id="s1", platform="mattermost")

    first = registration_gate.handle(text="hello", **kwargs)
    second = registration_gate.handle(text="김민영님", **kwargs)
    third = registration_gate.handle(text="간단하게, 결론부터", **kwargs)

    assert (first.state, second.state, third.state) == ("GREETED", "BASIC_READY", "SESSION_OK")
    assert third.allowed is False
    fourth = registration_gate.handle(text="구글 워크스페이스 연동 시작", **kwargs)
    assert fourth.allowed is True
    assert fourth.state == "SESSION_OK"


def test_registration_gate_does_not_allow_oauth_before_session_ok(monkeypatch):
    from control_plane import registration_gate

    monkeypatch.delenv("OAOS_ENV", raising=False)
    monkeypatch.setattr(registration_gate, "_MAPPING_PROVIDER", lambda tenant, user: {
        "employee_principal": user,
        "agent_id": "agent:assistant:kim",
        "status": "active",
    }, raising=False)
    registration_gate._memory.clear()
    result = registration_gate.handle(
        tenant_id="t1", user_id="employee:kim", session_id="s1",
        text="구글 워크스페이스 연동 시작", platform="mattermost",
    )
    assert result.state == "GREETED"
    assert result.allowed is False
    assert "세션" in result.response or "설정" in result.response
