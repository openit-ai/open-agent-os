"""Test output_type validation with retry — pydantic-ai inspired pattern."""
from __future__ import annotations
import pytest
from pydantic import BaseModel

from agent_runtime.llm_runtime import LLMProviderAdapter, ToolOutputLimits, OAOSContext
from agent_runtime.session import SessionManager


class UserProfile(BaseModel):
    name: str
    age: int
    email: str


@pytest.mark.asyncio
async def test_output_type_validation_with_retry():
    """output_type: BaseModel support with validation and retry (max 2).

    Adapter should accept output_type=BaseModel, validate LLM JSON output,
    retry up to 2 times on validation failure with correction prompt.
    """
    adapter = LLMProviderAdapter(model="mock-model", timeout_s=5.0, max_retries=1)

    # Queue: first response invalid (missing email), second valid — tests retry
    invalid_resp = {
        "id": "mock-1",
        "object": "chat.completion",
        "model": "mock-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"name": "Alice", "age": 30}'}, "finish_reason": "stop"}],
        "usage": {},
    }
    valid_resp = {
        "id": "mock-2",
        "object": "chat.completion",
        "model": "mock-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"name": "Alice", "age": 30, "email": "alice@example.com"}'}, "finish_reason": "stop"}],
        "usage": {},
    }
    adapter.push_mock_response(invalid_resp)
    adapter.push_mock_response(valid_resp)

    messages = [{"role": "user", "content": "Return user profile as JSON"}]
    ctx = OAOSContext(tenant_id="t1", agent_id="a1", trace_id="trace-test-1", vault_path="vault/t1/a1", policy={"allow": True})

    result = await adapter.completion(messages, output_type=UserProfile, trace_id=ctx.trace_id, oaos_context=ctx)

    # After retry, should be validated
    assert result.get("_output_type_validated") is True
    parsed = result.get("_parsed_output")
    assert parsed is not None
    assert isinstance(parsed, UserProfile)
    assert parsed.name == "Alice"
    assert parsed.age == 30
    assert parsed.email == "alice@example.com"
    # Original history should not be mutated in place but retry occurred (mock index advanced)
    assert adapter._mock_index == 2

    # Also verify ToolOutputLimits truncate at 4000
    limits = ToolOutputLimits(truncate_at=4000)
    long_content = "x" * 5000
    limited, should_retry, err = limits.apply(long_content)
    assert len(limited) <= 4000 + len(limits.suffix_on_truncate)
    assert limited.endswith(limits.suffix_on_truncate)

    # JSON schema check — valid passes, missing required triggers retry flag
    schema = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
    valid_json = '{"name": "ok"}'
    _, retry_ok, _ = limits.apply(valid_json, json_schema=schema)
    assert retry_ok is False
    invalid_json = '{"age": 30}'
    _, retry_bad, err_msg = limits.apply(invalid_json, json_schema=schema)
    assert retry_bad is True
    assert "missing required field" in err_msg.lower()

    # OAOSContext injection check — context carries vault_path
    assert ctx.vault_path == "vault/t1/a1"
    assert ctx.tenant_id == "t1"
    assert ctx.policy == {"allow": True}

    # SessionManager helper
    mgr = SessionManager()
    sess = mgr.create(tenant_id="t1", agent_id="a1", user_id="u1")
    oaos = mgr.get_oaos_context(sess["session_id"], "t1", "a1", policy={"role": "user"})
    assert oaos.tenant_id == "t1"
    assert oaos.agent_id == "a1"
    assert oaos.vault_path == "vault/t1/a1"
    assert oaos.policy == {"role": "user"}
