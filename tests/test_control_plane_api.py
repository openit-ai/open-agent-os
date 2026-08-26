"""FastAPI integration — header-based isolation."""
from fastapi.testclient import TestClient
from control_plane.app import app

client = TestClient(app)

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

def test_create_and_get_session():
    # create
    r = client.post("/v1/sessions", json={"tenant_id": "t1", "user_id": "employee:kim"}, headers={"X-User-Id": "employee:kim"})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert r.json()["agent_id"] == "agent:assistant:kim"
    trace = r.json()["trace_id"]
    # get as owner
    r2 = client.get(f"/v1/sessions/{sid}", headers={"X-User-Id": "employee:kim"})
    assert r2.status_code == 200
    assert r2.json()["trace_id"] == trace
    # cross-user denied
    r3 = client.get(f"/v1/sessions/{sid}", headers={"X-User-Id": "employee:lee"})
    assert r3.status_code == 403

def test_send_prompt_and_context():
    r = client.post("/v1/sessions", json={"tenant_id": "t1", "user_id": "employee:kim"}, headers={"X-User-Id": "employee:kim"})
    sid = r.json()["session_id"]
    rp = client.post(f"/v1/sessions/{sid}/prompt", json={"session_id": sid, "prompt": "오늘 할 일 정리해줘"}, headers={"X-User-Id": "employee:kim"})
    assert rp.status_code == 200
    assert "request_id" in rp.json()
    # context preserved
    rc = client.get(f"/v1/context/{sid}", headers={"X-User-Id": "employee:kim"})
    assert rc.status_code == 200
    assert rc.json()["user_id"] == "employee:kim"
    assert rc.json()["session_id"] == sid

def test_mattermost_webhook():
    r = client.post("/v1/mattermost/events", json={"tenant_id": "t1", "user_id": "employee:park", "text": "오늘 일정 알려줘"})
    assert r.status_code == 200, r.text
    assert r.json()["received"] is True
    assert r.json()["agent_id"] == "agent:assistant:park"
    assert "session_id" in r.json()

def test_stream_sse():
    r = client.post("/v1/sessions", json={"tenant_id": "t1", "user_id": "employee:kim"}, headers={"X-User-Id": "employee:kim"})
    sid = r.json()["session_id"]
    # need a prompt so stream has content
    client.post(f"/v1/sessions/{sid}/prompt", json={"session_id": sid, "prompt": "hi"}, headers={"X-User-Id": "employee:kim"})
    rs = client.get(f"/v1/sessions/{sid}/stream", headers={"X-User-Id": "employee:kim"})
    assert rs.status_code == 200
    assert "text/event-stream" in rs.headers["content-type"]
    body = rs.text
    assert "data:" in body
    assert "done" in body or "token" in body
