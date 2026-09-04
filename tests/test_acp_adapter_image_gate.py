"""ACP image gate — non-image refs must never become image_url parts.

Regression for TXT E2E timeout: build_llm_messages previously added every
attachment_ref/file_id as image_url, so JSON/TXT/PDF refs were sent as
image_url/file:// and the CP 200 never produced a new answer.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control-plane"))

from control_plane.acp_adapter import ACPAdapter
from control_plane.session import SessionRecord

POLICY = {
    "conclusion_first": False,
    "verbosity": "medium",
    "technical_depth": "medium",
    "evidence_requirement": "medium",
    "challenge_assumptions": False,
    "alternatives": 1,
    "confirmation_level": "medium",
}


def _sess() -> SessionRecord:
    return SessionRecord(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        agent_id="a1",
        trace_id="tr1",
        security_domain="general",
    )


def _parts(msgs) -> list:
    content = msgs[1]["content"]
    return content if isinstance(content, list) else [{"type": "text", "text": content}]


def _image_urls(parts) -> list:
    return [p for p in parts if p.get("type") == "image_url"]


def test_non_image_text_preview_creates_no_image_url():
    ad = ACPAdapter("http://localhost:8642")
    msgs = ad.build_llm_messages(
        _sess(),
        "hello [preview]",
        policy=dict(POLICY),
        file_ids=["fid-txt"],
        attachment_refs=[{
            "file_id": "fid-txt", "attachment_id": "fid-txt",
            "kind": "text_preview", "mime_type": "text/plain",
            "filename": "note.txt", "preview": "hello",
        }],
    )
    parts = _parts(msgs)
    assert _image_urls(parts) == []
    assert parts[0]["type"] == "text"


def test_non_image_stored_only_pdf_creates_no_image_url():
    ad = ACPAdapter("http://localhost:8642")
    msgs = ad.build_llm_messages(
        _sess(),
        "hello",
        policy=dict(POLICY),
        file_ids=["fid-pdf"],
        attachment_refs=[{
            "file_id": "fid-pdf", "kind": "stored_only",
            "mime_type": "application/pdf", "filename": "doc.pdf",
        }],
    )
    assert _image_urls(_parts(msgs)) == []


def test_kindless_non_image_mime_creates_no_image_url():
    ad = ACPAdapter("http://localhost:8642")
    msgs = ad.build_llm_messages(
        _sess(),
        "hello",
        policy=dict(POLICY),
        file_ids=["fid-json"],
        attachment_refs=[{
            "file_id": "fid-json", "mime_type": "application/json",
            "filename": "data.json",
        }],
    )
    assert _image_urls(_parts(msgs)) == []


def test_image_ref_creates_image_url():
    ad = ACPAdapter("http://localhost:8642")
    msgs = ad.build_llm_messages(
        _sess(),
        "see image",
        policy=dict(POLICY),
        file_ids=["fid-img"],
        attachment_refs=[{
            "file_id": "fid-img", "kind": "image",
            "mime_type": "image/png", "filename": "a.png",
            "data_url": "data:image/png;base64,iVBORw0KGgo=",
        }],
    )
    urls = _image_urls(_parts(msgs))
    assert len(urls) == 1
    assert urls[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_image_ref_without_bytes_falls_back_to_file_url():
    ad = ACPAdapter("http://localhost:8642")
    msgs = ad.build_llm_messages(
        _sess(),
        "see image",
        policy=dict(POLICY),
        file_ids=["fid-img2"],
        attachment_refs=[{
            "file_id": "fid-img2", "kind": "image",
            "mime_type": "image/png", "filename": "a.png",
        }],
    )
    urls = _image_urls(_parts(msgs))
    assert len(urls) == 1
    assert urls[0]["image_url"]["url"] == "file://fid-img2"


def test_file_ids_only_without_refs_keeps_compat():
    ad = ACPAdapter("http://localhost:8642")
    msgs = ad.build_llm_messages(
        _sess(), "hello", policy=dict(POLICY),
        file_ids=["fid-only"], attachment_refs=None,
    )
    urls = _image_urls(_parts(msgs))
    assert len(urls) == 1
