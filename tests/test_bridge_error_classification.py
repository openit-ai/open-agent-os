from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.error import HTTPError

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "oaos-mm-bridge.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("oaos_bridge_error_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_permanent_cp_errors_are_not_retryable():
    bridge = load_bridge()
    for status in (400, 401, 403, 404, 405, 409, 422):
        error = HTTPError("http://cp", status, "rejected", {}, None)
        assert bridge._is_permanent_cp_error(error) is True


def test_transient_cp_errors_are_retryable():
    bridge = load_bridge()
    for status in (408, 429, 500, 502, 503, 504):
        error = HTTPError("http://cp", status, "retry", {}, None)
        assert bridge._is_permanent_cp_error(error) is False


def test_user_messages_are_specific_but_safe():
    bridge = load_bridge()
    assert "등록" in bridge._cp_user_message(403, "OAOS user registration required")
    assert "권한" in bridge._cp_user_message(403, "permission denied")
    assert "인증" in bridge._cp_user_message(401, "secret internal detail")
    assert "요청 형식" in bridge._cp_user_message(400, "validation traceback")
    assert "요청이 많습니다" in bridge._cp_user_message(429, "quota internals")
    assert "traceback" not in bridge._cp_user_message(500, "traceback with secret")


def test_registration_notice_requires_explicit_message():
    bridge = load_bridge()
    calls = []
    bridge.api_post = lambda path, body: calls.append((path, body))
    bridge._post_registration_notice("channel", "root", "안전한 안내")
    assert calls == [("/api/v4/posts", {"channel_id": "channel", "root_id": "root", "message": "안전한 안내"})]


def test_attachment_router_text_previewable():
    bridge = load_bridge()
    assert bridge._is_text_previewable("application/json", "data.json") is True
    assert bridge._is_text_previewable("text/plain", "note.txt") is True
    assert bridge._is_text_previewable("application/pdf", "doc.pdf") is False
    assert bridge._is_text_previewable("audio/mpeg", "song.mp3") is False


def test_attachment_router_masks_secrets():
    bridge = load_bridge()
    masked = bridge._mask_secrets('{"api_key": "abc123", "name": "x"}')
    assert "abc123" not in masked
    assert "***" in masked


def test_attachment_router_sensitive_filename_blocked():
    bridge = load_bridge()
    ref = bridge._build_non_image_ref("abc123", "client_secret_123.json", "application/json", 100)
    assert ref["kind"] == "stored_only"
    assert ref["reason"] == "sensitive"
    assert "preview" not in ref


def test_attachment_router_unsupported_has_no_bytes():
    bridge = load_bridge()
    ref = bridge._build_non_image_ref("abc123", "doc.pdf", "application/pdf", 100)
    assert ref["kind"] == "stored_only"
    assert "preview" not in ref
    assert "base64" not in ref
    assert "data_url" not in ref


def test_format_400_message_guides_attachment():
    bridge = load_bridge()
    msg = bridge._cp_user_message(400, "text/message required (or file_ids/attachment_refs for image)")
    assert "이미지" in msg
    assert "traceback" not in msg.lower()
