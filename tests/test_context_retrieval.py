from __future__ import annotations

from control_plane.context_retrieval import classify_context_route, format_context


def test_route_classification():
    assert classify_context_route("내 일정 알려줘") == "personal"
    assert classify_context_route("회사 정책 찾아줘") == "enterprise"
    assert classify_context_route("안녕하세요") is None


def test_context_format_preserves_source():
    result = format_context("enterprise", [{"source_uri": "outline/doc-1", "text": "정책 내용"}])
    assert "outline/doc-1" in result
    assert "정책 내용" in result
    assert "근거가 부족하면" in result
