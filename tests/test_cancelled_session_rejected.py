from __future__ import annotations

from control_plane.session import InMemorySessionStore


def test_cancelled_session_is_not_reusable_for_owner_lookup() -> None:
    store = InMemorySessionStore()
    record = store.create("default", "employee:kim", "agent:assistant:kim")
    store.cancel(record.session_id, "employee:kim")
    assert store.get(record.session_id, "employee:kim").status == "cancelled"
    assert store.find_latest_for_owner("default", "employee:kim") is None
