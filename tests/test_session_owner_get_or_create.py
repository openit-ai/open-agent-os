from __future__ import annotations

from control_plane.session import InMemorySessionStore


def test_get_or_create_reuses_only_active_owner_session() -> None:
    store = InMemorySessionStore()
    first = store.get_or_create_for_owner("default", "employee:kim", "agent:assistant:kim")
    second = store.get_or_create_for_owner("default", "employee:kim", "agent:assistant:kim")
    assert second.session_id == first.session_id
    assert len(store) == 1


def test_get_or_create_does_not_reuse_cancelled_session() -> None:
    store = InMemorySessionStore()
    first = store.get_or_create_for_owner("default", "employee:kim", "agent:assistant:kim")
    store.cancel(first.session_id, "employee:kim")
    second = store.get_or_create_for_owner("default", "employee:kim", "agent:assistant:kim")
    assert second.session_id != first.session_id
    assert second.status == "active"
    assert len(store) == 2
