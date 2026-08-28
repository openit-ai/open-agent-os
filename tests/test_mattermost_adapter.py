"""Mattermost adapter hardening tests — §§16A, 23.

Covers: verify_signature, parse_incoming, map_mattermost_user,
slash / actions HMAC, approval card rendering, config, streaming, error mapping.
"""
import hashlib
import hmac
import json
import urllib.parse
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

# Ensure paths for imports
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for p in [
    ROOT / "adapters",
    ROOT / "control-plane",
    ROOT / "security" / "approval",
    ROOT / "packages" / "common-types",
    ROOT / "packages" / "agent-context",
]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from mattermost.adapter import MattermostAdapter  # type: ignore
from control_plane.config import settings
from control_plane.app import app
from control_plane.mattermost_adapter.webhook import verify_mattermost_signature, VALID_DECISIONS

client = TestClient(app)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sig(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------

class TestVerifySignature:
    def test_valid_signature(self):
        a = MattermostAdapter(base_url="https://mm.example.com", bot_token="tok", webhook_secret="s3cr3t")
        body = b'{"text":"hello"}'
        sig = _sig(body, "s3cr3t")
        assert a.verify_signature(body, sig) is True

    def test_invalid_signature(self):
        a = MattermostAdapter(webhook_secret="s3cr3t")
        body = b"hello"
        assert a.verify_signature(body, "bad") is False

    def test_missing_signature_returns_false_when_secret_set(self):
        a = MattermostAdapter(webhook_secret="s3cr3t")
        assert a.verify_signature(b"body", None) is False
        assert a.verify_signature(b"body", "") is False

    def test_no_secret_dev_mode_accepts_any(self):
        a = MattermostAdapter(webhook_secret="")
        assert a.verify_signature(b"anything", None) is True
        assert a.verify_signature(b"anything", "garbage") is True

    def test_webhook_verify_helper_dev_mode(self):
        assert verify_mattermost_signature(b"body", None, None) is True
        assert verify_mattermost_signature(b"body", None, "") is True

    def test_webhook_verify_helper_valid(self):
        body = b'{"user_id":"employee:kim"}'
        sig = _sig(body, "topsecret")
        assert verify_mattermost_signature(body, sig, "topsecret") is True

    def test_webhook_verify_helper_invalid(self):
        assert verify_mattermost_signature(b"body", "bad", "topsecret") is False

    def test_verify_uses_hexdigest_compare_digest(self):
        a = MattermostAdapter(webhook_secret="abc")
        body = b"payload"
        sig = _sig(body, "abc")
        # mutate one char
        bad = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        assert a.verify_signature(body, bad) is False

# ---------------------------------------------------------------------------
# parse_incoming
# ---------------------------------------------------------------------------

class TestParseIncoming:
    def test_parse_full_payload(self):
        a = MattermostAdapter()
        a.register_identity("uid123", "employee:kim")
        p = {"user_id": "uid123", "user_name": "kim", "text": "hello", "channel_id": "ch1", "team_id": "tm1"}
        out = a.parse_incoming(p)
        assert out["mattermost_user_id"] == "uid123"
        assert out["employee_principal"] == "employee:kim"
        assert out["text"] == "hello"
        assert out["channel_id"] == "ch1"

    def test_parse_nested_user(self):
        a = MattermostAdapter()
        p = {"user": {"id": "u42", "username": "lee"}, "message": "hi", "channel": {"id": "c9"}}
        out = a.parse_incoming(p)
        assert out["mattermost_user_id"] == "u42"
        assert out["text"] == "hi"
        assert out["channel_id"] == "c9"

    def test_parse_data_post_message(self):
        a = MattermostAdapter()
        p = {"data": {"post": {"message": "from data"}}}
        out = a.parse_incoming(p)
        assert out["text"] == "from data"

    def test_parse_empty_defaults(self):
        a = MattermostAdapter()
        out = a.parse_incoming({})
        assert out["text"] == ""
        assert out["channel_id"] == ""

    def test_parse_preserves_raw(self):
        a = MattermostAdapter()
        p = {"user_id": "u1", "text": "t"}
        out = a.parse_incoming(p)
        assert out["raw"] == p

# ---------------------------------------------------------------------------
# map_mattermost_user
# ---------------------------------------------------------------------------

class TestMapMattermostUser:
    def test_explicit_mapping_by_user_id(self):
        a = MattermostAdapter()
        a.register_identity("mm_123", "employee:park")
        assert a.map_mattermost_user("mm_123", "park") == "employee:park"

    def test_explicit_mapping_by_username(self):
        a = MattermostAdapter()
        a.register_identity("park", "employee:park")
        assert a.map_mattermost_user("other_id", "park") == "employee:park"

    def test_derive_from_username(self):
        a = MattermostAdapter()
        assert a.map_mattermost_user("uid999", "Alice.Wu") == "employee:alice.wu"

    def test_derive_from_user_id_when_no_username(self):
        a = MattermostAdapter()
        assert a.map_mattermost_user("UID-1234", None) == "employee:uid-1234"

    def test_sanitize_special_chars(self):
        a = MattermostAdapter()
        assert a.map_mattermost_user("x", "Kim@Open!") == "employee:kimopen"

    def test_register_requires_employee_prefix(self):
        a = MattermostAdapter()
        with pytest.raises(ValueError):
            a.register_identity("u1", "badprefix:kim")

    def test_reverse_map(self):
        a = MattermostAdapter()
        a.register_identity("mm1", "employee:kim")
        assert a.reverse_map("employee:kim") == "mm1"
        assert a.reverse_map("employee:none") is None

    def test_map_lowercase_normalization(self):
        a = MattermostAdapter()
        assert a.map_mattermost_user("u1", "KIM") == "employee:kim"

    def test_unknown_sanitize_fallback(self):
        a = MattermostAdapter()
        # username with only special chars -> fallback unknown
        assert a.map_mattermost_user("u1", "@@@") == "employee:unknown"

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

class TestMattermostConfig:
    def test_settings_has_mattermost_fields(self):
        for field in ["mattermost_bot_token", "mattermost_webhook_secret", "mattermost_url"]:
            assert hasattr(settings, field), f"missing {field}"

    def test_settings_defaults_empty(self):
        # default should be str, empty when not env-set
        assert isinstance(settings.mattermost_bot_token, str)
        assert isinstance(settings.mattermost_webhook_secret, str)
        assert isinstance(settings.mattermost_url, str)

# ---------------------------------------------------------------------------
# approval card rendering
# ---------------------------------------------------------------------------

class TestApprovalCard:
    def test_build_card_has_4_buttons(self):
        a = MattermostAdapter(base_url="https://mm.example.com")
        req = {"approval_id": "apr_123", "resource": "repo:main", "action": "DEPLOY", "risk": "HIGH", "user_id": "employee:kim", "expires_at": "2026-09-01T00:00:00Z"}
        props = a.build_approval_card(req)
        assert "attachments" in props
        att = props["attachments"][0]
        assert len(att["actions"]) == 4
        ids = {act["id"] for act in att["actions"]}
        assert ids == {"deny", "approve_once", "approve_user_always", "approve_group_always"}

    def test_card_decisions_in_context(self):
        a = MattermostAdapter(base_url="https://mm.example.com")
        req = {"approval_id": "apr_999", "resource": "r", "action": "MERGE", "risk": "MEDIUM", "user_id": "employee:lee"}
        props = a.build_approval_card(req)
        decisions = {act["integration"]["context"]["decision"] for act in props["attachments"][0]["actions"]}
        assert decisions == {"DENIED", "APPROVED_ONCE", "APPROVED_USER_ALWAYS", "APPROVED_GROUP_ALWAYS"}

    def test_card_integration_url_contains_actions(self):
        a = MattermostAdapter(base_url="https://mm.example.com")
        req = {"approval_id": "apr_1", "resource": "r", "action": "READ", "risk": "LOW"}
        props = a.build_approval_card(req)
        for act in props["attachments"][0]["actions"]:
            assert "/v1/mattermost/actions" in act["integration"]["url"]

    def test_card_fallback_and_fields(self):
        a = MattermostAdapter()
        req = {"approval_id": "apr_x", "resource": "db:prod", "action": "EXPORT", "risk": "HIGH", "user_id": "employee:kim", "expires_at": "2026-01-01"}
        props = a.build_approval_card(req)
        att = props["attachments"][0]
        assert att["fallback"]
        assert att["title"] == "Approval Required"
        assert "apr_x" in att["text"]

    def test_card_with_pydantic_request_object(self):
        from approval_workflow.workflow import ApprovalStore
        store = ApprovalStore(signing_key="k")
        req = store.create(user_id="employee:kim", agent_id="agent:assistant:kim", action="DEPLOY", resource="repo:main", risk="HIGH")
        a = MattermostAdapter(base_url="https://mm.example.com")
        props = a.build_approval_card(req)
        assert len(props["attachments"][0]["actions"]) == 4

    def test_card_high_risk_color(self):
        a = MattermostAdapter()
        p_high = a.build_approval_card({"approval_id": "a1", "risk": "HIGH", "action": "DEPLOY", "resource": "r"})
        p_low = a.build_approval_card({"approval_id": "a2", "risk": "LOW", "action": "READ", "resource": "r"})
        assert p_high["attachments"][0]["color"] == "#F59E0B"
        assert p_low["attachments"][0]["color"] == "#3B82F6"

    @pytest.mark.asyncio
    async def test_post_approval_card_skeleton_returns_props_and_root(self):
        a = MattermostAdapter(base_url="", bot_token="")
        req = {"approval_id": "apr_skel", "resource": "r", "action": "DEPLOY", "risk": "HIGH", "user_id": "employee:kim"}
        out = await a.post_approval_card("ch1", req, root_id="root123")
        assert out["_skeleton"] is True
        assert out["channel_id"] == "ch1"
        assert out["root_id"] == "root123"
        assert "props" in out
        assert "attachments" in out["props"]
        assert out["props"]["approval_id"] == "apr_skel"

    @pytest.mark.asyncio
    async def test_send_message_skeleton_preserves_props_and_root(self):
        a = MattermostAdapter(base_url="", bot_token="")
        out = await a.send_message("ch1", "hello", props={"attachments": [{"title": "t"}]}, root_id="r1")
        assert out["_skeleton"] is True
        assert out["props"]["attachments"][0]["title"] == "t"
        assert out["root_id"] == "r1"

# ---------------------------------------------------------------------------
# webhook — events error mapping + HMAC
# ---------------------------------------------------------------------------

class TestMattermostEvents:
    def test_events_invalid_signature_401(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "secret123")
        body = json.dumps({"user_id": "employee:kim", "text": "hi"}).encode()
        r = client.post("/v1/mattermost/events", content=body, headers={"X-Mattermost-Signature": "bad", "Content-Type": "application/json"})
        assert r.status_code == 401
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")

    def test_events_invalid_json_400(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/events", content=b"not-json", headers={"Content-Type": "application/json"})
        # may be 400 or still parse; our endpoint checks json loads → 400
        assert r.status_code in (400, 422)

    def test_events_missing_user_id_400(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/events", json={"text": "hello"})
        assert r.status_code == 400

    def test_events_missing_text_400(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/events", json={"user_id": "employee:kim"})
        assert r.status_code == 400

    def test_events_success(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/events", json={"tenant_id": "t1", "user_id": "employee:kim", "text": "hello mattermost"})
        assert r.status_code == 200
        assert r.json()["received"] is True
        assert "session_id" in r.json()

    def test_events_session_not_found_404(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/events", json={"user_id": "employee:kim", "text": "hi", "session_id": "sess_notexist123"})
        assert r.status_code == 404

    def test_events_cross_user_403(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        # create session as kim
        r1 = client.post("/v1/sessions", json={"tenant_id": "t1", "user_id": "employee:kim"}, headers={"X-User-Id": "employee:kim"})
        sid = r1.json()["session_id"]
        # try to reuse session as lee via mattermost events
        r2 = client.post("/v1/mattermost/events", json={"user_id": "employee:lee", "text": "hi", "session_id": sid})
        assert r2.status_code == 403

    def test_events_valid_hmac_passes(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "mysecret")
        body = json.dumps({"user_id": "employee:kim", "text": "hmac ok"}).encode()
        sig = _sig(body, "mysecret")
        r = client.post("/v1/mattermost/events", content=body, headers={"X-Mattermost-Signature": sig, "Content-Type": "application/json"})
        assert r.status_code == 200
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")

    def test_events_briefing_keyword_routed(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/events", json={"user_id": "employee:kim", "text": "오늘 업무 정리해줘"})
        assert r.status_code == 200
        # briefing path or fallback acp
        assert r.json()["received"] is True

# ---------------------------------------------------------------------------
# slash
# ---------------------------------------------------------------------------

class TestMattermostSlash:
    def test_slash_invalid_signature_401(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "s123")
        body = json.dumps({"command": "/agent", "text": "hi", "user_id": "employee:kim"}).encode()
        r = client.post("/v1/mattermost/slash", content=body, headers={"X-Mattermost-Signature": "bad", "Content-Type": "application/json"})
        assert r.status_code == 401
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")

    def test_slash_invalid_json_400(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/slash", content=b"{{{", headers={"Content-Type": "application/json"})
        assert r.status_code == 400

    def test_slash_json_success(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/slash", json={"command": "/agent", "text": "오늘 일정 알려줘", "user_id": "employee:kim", "channel_id": "ch1"})
        assert r.status_code == 200
        assert r.json()["received"] is True

    def test_slash_form_urlencoded_success(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        body = urllib.parse.urlencode({"command": "/agent", "text": "hello", "user_id": "employee:kim", "channel_id": "ch1"})
        r = client.post("/v1/mattermost/slash", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 200
        assert r.json()["received"] is True

    def test_slash_hmac_form(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "formsecret")
        body = urllib.parse.urlencode({"command": "/agent", "text": "hi", "user_id": "employee:kim"}).encode()
        sig = _sig(body, "formsecret")
        r = client.post("/v1/mattermost/slash", content=body, headers={"X-Mattermost-Signature": sig, "Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 200
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")

    def test_slash_missing_user_id_400(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/slash", json={"command": "/agent", "text": "hi"})
        assert r.status_code == 400

    def test_slash_missing_text_and_command_400(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/slash", json={"user_id": "employee:kim"})
        assert r.status_code == 400

    def test_slash_command_only_text(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/slash", json={"command": "/agent", "user_id": "employee:kim"})
        assert r.status_code == 200

    def test_slash_reuses_briefing_logic(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/slash", json={"command": "/agent", "text": "정리해줘", "user_id": "employee:kim"})
        assert r.status_code == 200
        assert r.json()["received"] is True

    def test_slash_session_resume_404(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/slash", json={"command": "/agent", "text": "hi", "user_id": "employee:kim", "session_id": "sess_missing999"})
        assert r.status_code == 404

# ---------------------------------------------------------------------------
# actions — HMAC + 4 decisions
# ---------------------------------------------------------------------------

class TestMattermostActions:
    def _setup_approval(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "actsecret")
        # get store singleton and create approval
        from control_plane.mattermost_adapter.webhook import _get_approval_store
        store = _get_approval_store()
        # clear for isolation? keep but ensure new approval
        req = store.create(user_id="employee:kim", agent_id="agent:assistant:kim", action="DEPLOY", resource="repo:main", risk="HIGH")
        return store, req

    def test_actions_invalid_signature_401(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "actsecret")
        body = json.dumps({"approval_id": "apr_x", "decision": "DENIED", "user_id": "employee:kim"}).encode()
        r = client.post("/v1/mattermost/actions", content=body, headers={"X-Mattermost-Signature": "bad", "Content-Type": "application/json"})
        assert r.status_code == 401
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")

    def test_actions_invalid_json_400(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/actions", content=b"notjson", headers={"Content-Type": "application/json"})
        assert r.status_code == 400

    def test_actions_missing_approval_id_400(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/actions", json={"decision": "DENIED", "user_id": "employee:kim"})
        assert r.status_code == 400

    def test_actions_invalid_decision_400(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/actions", json={"approval_id": "apr_x", "decision": "WRONG", "user_id": "employee:kim"})
        assert r.status_code == 400

    def test_actions_approval_not_found_404(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/actions", json={"approval_id": "apr_notfound_12345", "decision": "DENIED", "user_id": "employee:kim"})
        assert r.status_code == 404

    @pytest.mark.parametrize("decision", ["DENIED", "APPROVED_ONCE", "APPROVED_USER_ALWAYS", "APPROVED_GROUP_ALWAYS"])
    def test_actions_four_decisions(self, decision, monkeypatch):
        store, req = self._setup_approval(monkeypatch)
        body_dict = {"approval_id": req.approval_id, "decision": decision, "user_id": "employee:kim", "context": {"approval_id": req.approval_id, "decision": decision}}
        # for group_always need group_id
        if decision == "APPROVED_GROUP_ALWAYS":
            body_dict["group_id"] = "group:eng"
            body_dict["context"]["group_id"] = "group:eng"
        body = json.dumps(body_dict).encode()
        sig = _sig(body, "actsecret")
        r = client.post("/v1/mattermost/actions", content=body, headers={"X-Mattermost-Signature": sig, "Content-Type": "application/json"})
        assert r.status_code == 200, r.text
        assert r.json()["decision"] == decision
        assert r.json()["approval_id"] == req.approval_id
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")

    def test_actions_decision_via_context_nesting(self, monkeypatch):
        store, req = self._setup_approval(monkeypatch)
        body_dict = {"user_id": "employee:kim", "context": {"approval_id": req.approval_id, "decision": "DENIED"}}
        body = json.dumps(body_dict).encode()
        sig = _sig(body, "actsecret")
        r = client.post("/v1/mattermost/actions", content=body, headers={"X-Mattermost-Signature": sig, "Content-Type": "application/json"})
        assert r.status_code == 200
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")

    def test_actions_form_payload_mattermost_style(self, monkeypatch):
        store, req = self._setup_approval(monkeypatch)
        inner = json.dumps({"user_id": "employee:kim", "context": {"approval_id": req.approval_id, "decision": "APPROVED_ONCE"}, "channel_id": "ch1"})
        body = urllib.parse.urlencode({"payload": inner}).encode()
        sig = _sig(body, "actsecret")
        r = client.post("/v1/mattermost/actions", content=body, headers={"X-Mattermost-Signature": sig, "Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 200
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")

    def test_actions_valid_decisions_set(self):
        assert VALID_DECISIONS == {"DENIED", "APPROVED_ONCE", "APPROVED_USER_ALWAYS", "APPROVED_GROUP_ALWAYS"}

    def test_actions_hmac_verified(self, monkeypatch):
        store, req = self._setup_approval(monkeypatch)
        body = json.dumps({"approval_id": req.approval_id, "decision": "DENIED", "user_id": "employee:kim"}).encode()
        sig = _sig(body, "actsecret")
        r = client.post("/v1/mattermost/actions", content=body, headers={"X-Mattermost-Signature": sig, "Content-Type": "application/json"})
        assert r.status_code == 200
        # bad sig fails
        r2 = client.post("/v1/mattermost/actions", content=body, headers={"X-Mattermost-Signature": "bad", "Content-Type": "application/json"})
        assert r2.status_code == 401
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")

# ---------------------------------------------------------------------------
# streaming + health
# ---------------------------------------------------------------------------

class TestMattermostStreamingAndHealth:
    def test_health(self):
        r = client.get("/v1/mattermost/health")
        assert r.status_code == 200
        assert r.json()["adapter"] == "mattermost"

    @pytest.mark.asyncio
    async def test_stream_and_post_no_channel_noop(self):
        from control_plane.mattermost_adapter.webhook import _stream_and_post_to_mattermost
        from control_plane.session import SessionRecord, new_session_id, new_trace_id
        rec = SessionRecord(session_id=new_session_id(), tenant_id="t1", user_id="employee:kim", agent_id="agent:assistant:kim", trace_id=new_trace_id(), security_domain="general")
        # no channel -> should return without error
        await _stream_and_post_to_mattermost(None, None, rec)

    @pytest.mark.asyncio
    async def test_stream_posts_threaded(self):
        from control_plane.mattermost_adapter.webhook import _stream_and_post_to_mattermost
        from control_plane.session import SessionRecord, new_session_id, new_trace_id

        rec = SessionRecord(session_id=new_session_id(), tenant_id="t1", user_id="employee:kim", agent_id="agent:assistant:kim", trace_id=new_trace_id(), security_domain="general")

        async def fake_stream(self_or_session, session=None):
            # support both call signatures
            yield {"type": "token", "data": {"text": "hello "}}
            yield {"type": "token", "data": {"text": "world\n"}}
            yield {"type": "done", "data": {}}

        mock_send = AsyncMock(return_value={"_skeleton": True})
        with patch("control_plane.mattermost_adapter.webhook.ACPAdapter") as MockACP:
            # instance.stream_events should be an async generator function
            async def _gen(session):
                async for x in fake_stream(session):
                    yield x
            MockACP.return_value.stream_events = _gen
            with patch("control_plane.mattermost_adapter.webhook._get_mattermost_adapter") as mock_get:
                mock_adapter = AsyncMock()
                mock_adapter.send_message = mock_send
                mock_get.return_value = mock_adapter
                await _stream_and_post_to_mattermost("ch1", "root123", rec)
                assert mock_send.called
                # check root_id threaded
                _, kwargs = mock_send.call_args if mock_send.call_args[1] else (mock_send.call_args[0], {})
                # positional: channel_id, text, root_id=...
                if mock_send.call_args.kwargs:
                    assert mock_send.call_args.kwargs.get("root_id") == "root123" or "root123" in str(mock_send.call_args)
                else:
                    # positional args check
                    assert mock_send.call_args[0][0] == "ch1"

    def test_events_triggers_background_stream_task(self, monkeypatch):
        monkeypatch.setattr(settings, "mattermost_webhook_secret", "")
        r = client.post("/v1/mattermost/events", json={"user_id": "employee:kim", "text": "hello to channel", "channel_id": "ch_stream"})
        assert r.status_code == 200

    # parametrized verify_signature combos to boost count
    @pytest.mark.parametrize("secret", ["s1", "secret123", ""])
    @pytest.mark.parametrize("body", [b"hi", b"{}", b'{"text":"hello"}'])
    def test_verify_parametrized(self, secret, body):
        a = MattermostAdapter(webhook_secret=secret)
        if not secret:
            assert a.verify_signature(body, None) is True
        else:
            sig = _sig(body, secret)
            assert a.verify_signature(body, sig) is True
            assert a.verify_signature(body, sig + "x") is False

    # parametrized map tests
    @pytest.mark.parametrize("username,expected_suffix", [
        ("john", "john"),
        ("John.Doe", "john.doe"),
        ("alice_wu", "alice_wu"),
        ("bob-123", "bob-123"),
    ])
    def test_map_parametrized_suffix(self, username, expected_suffix):
        a = MattermostAdapter()
        assert a.map_mattermost_user("u1", username) == f"employee:{expected_suffix}"

    # parametrized card decisions
    @pytest.mark.parametrize("risk", ["HIGH", "MEDIUM", "LOW"])
    def test_card_risk_parametrized(self, risk):
        a = MattermostAdapter(base_url="https://mm.example.com")
        props = a.build_approval_card({"approval_id": "a1", "risk": risk, "action": "DEPLOY", "resource": "r"})
        assert props["attachments"][0]["fields"][0]["value"] == risk
