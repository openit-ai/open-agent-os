import asyncio
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control-plane"))
from control_plane.acp_adapter import _llm_max_attempts, _with_retry_acp


def test_production_defaults_to_one_attempt(monkeypatch):
    monkeypatch.setenv("OAOS_ENV", "production")
    monkeypatch.delenv("OAOS_LLM_MAX_ATTEMPTS", raising=False)
    assert _llm_max_attempts() == 1


def test_explicit_attempt_override_is_bounded(monkeypatch):
    monkeypatch.setenv("OAOS_LLM_MAX_ATTEMPTS", "99")
    assert _llm_max_attempts() == 3


def test_retry_helper_attempt_count_is_explicit(monkeypatch):
    calls = 0
    async def failing():
        nonlocal calls
        calls += 1
        raise RuntimeError("permanent")
    monkeypatch.setattr("control_plane.acp_adapter._is_retryable_status", lambda exc: True)
    try:
        asyncio.run(_with_retry_acp(failing, max_retries=0, trace_id="t"))
    except RuntimeError:
        pass
    assert calls == 1
