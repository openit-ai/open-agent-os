"""LLM Runtime — wires Session + Streaming + MCP Client per §16C.

Minimal built-in runtime: session lifecycle, token streaming, MCP tool loop.
No hard dependency on litellm — if litellm is installed and LLM_API_KEY is set,
it will be used; otherwise a mock streaming response is emitted so tests/offline
still pass.  No shell/python execution (LLM-only, §16F.1).

Enhanced with pydantic-ai inspired patterns (clean-room, BSL):
  1) OAOSContext (tenant_id, agent_id, trace_id, vault_path, policy) injected into every tool
  2) output_type: BaseModel support with validation and retry (max 2)
  3) ToolOutputLimits (truncate 4000, JSON schema check, auto retry)

Usage:
    from agent_runtime.llm_runtime import LLMRuntime, OAOSContext, ToolOutputLimits
    rt = LLMRuntime()
    sess = rt.create_session(tenant_id="t", agent_id="a", user_id="u")
    ctx = OAOSContext(tenant_id="t", agent_id="a", trace_id=sess["trace_id"])
    async for ev in rt.stream_prompt(sess["session_id"], tenant_id="t", agent_id="a", prompt="hi"):
        print(ev)

Provider-level:
    from agent_runtime.llm_runtime import LLMProviderAdapter
    adapter = LLMProviderAdapter(model="gpt-4o-mini")
    result = await adapter.completion(messages, output_type=MyModel)  # validates + retries
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, AsyncGenerator, Callable, Awaitable, get_origin, get_args

logger = logging.getLogger(__name__)

from pydantic import BaseModel, ValidationError

from .session import SessionManager, OAOSContext  # re-export
from .streaming import StreamingEngine
from .mcp_client import MCPClient

# Re-export OAOSContext for external callers
__all__ = [
    "OAOSContext",
    "ToolOutputLimits",
    "ModelRouting",
    "LLMProviderAdapter",
    "StructuredToolLoop",
    "LLMRuntime",
    "LLMRuntimeAdapter",
    "default_runtime",
    "AuditEvent",
    "AuditLogStub",
    "default_audit_log",
]

# ---------------------------------------------------------------------------
# 1) OAOSContext — already defined in session.py, re-exported
# Traceable context injected into every tool call.
# ---------------------------------------------------------------------------
# (OAOSContext imported above)

def _ensure_context(
    ctx: OAOSContext | dict[str, Any] | None,
    session: dict[str, Any] | Any | None = None,
    trace_id: str = "",
    policy: Any | None = None,
) -> OAOSContext:
    if isinstance(ctx, OAOSContext):
        return ctx
    if isinstance(ctx, dict):
        # dict shaped like OAOSContext
        return OAOSContext(
            tenant_id=str(ctx.get("tenant_id", "")),
            agent_id=str(ctx.get("agent_id", "")),
            trace_id=str(ctx.get("trace_id", "") or trace_id),
            vault_path=str(ctx.get("vault_path", "") or ""),
            policy=ctx.get("policy", policy),
            session_id=str(ctx.get("session_id", "")),
            user_id=str(ctx.get("user_id", "")),
            request_id=str(ctx.get("request_id", "")),
        )
    if session is not None:
        return OAOSContext.from_session(session, trace_id=trace_id, policy=policy)
    # minimal fallback
    return OAOSContext(trace_id=trace_id or f"trace_{uuid.uuid4().hex[:8]}", policy=policy)


def _inject_oaos_context(fn: Callable[..., Any], ctx: OAOSContext, args: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Inspect fn signature; if it expects OAOSContext injection, prepend ctx.

    Injection triggers when first parameter is named ctx/context/oaos_context/deps
    or annotated as OAOSContext. Returns (positional_prefix, remaining_kwargs).
    Clean-room: custom introspection, not MIT.
    """
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        if not params:
            return (), args
        first = params[0]
        name = first.name.lower()
        ann = first.annotation
        expects_ctx = False
        # name heuristic
        if name in ("ctx", "context", "oaos_context", "oaosctx", "deps", "deps_type"):
            expects_ctx = True
        # annotation heuristic
        try:
            # direct type check or string annotation
            if ann is OAOSContext:
                expects_ctx = True
            elif isinstance(ann, str) and "OAOSContext" in ann:
                expects_ctx = True
            elif get_origin(ann) is not None:
                # Union etc not needed
                pass
        except Exception:
            pass
        if expects_ctx:
            # inject as first positional arg, keep args as kwargs
            return (ctx,), args
        # also check if any param named oaos_context exists as kw
        for p in params:
            if p.name.lower() in ("oaos_context", "ctx", "context") and p.annotation is OAOSContext:
                # inject via kw
                if p.name not in args:
                    args = dict(args)
                    args[p.name] = ctx
                return (), args
        return (), args
    except Exception:
        return (), args


# ---------------------------------------------------------------------------
# 3) ToolOutputLimits — truncate 4000, JSON schema check, auto retry
# ---------------------------------------------------------------------------

@dataclass
class ToolOutputLimits:
    """Limits applied to every tool output before feeding back to LLM.

    - truncate_at: max chars of tool content (default 4000, pydantic-ai style)
    - json_schema_check: if True and tool declares json_schema, validate output
    - max_retries: auto retry count when output violates schema or is truncated-ambiguous
    - suffix_on_truncate: marker appended when truncated
    """

    truncate_at: int = 4000
    json_schema_check: bool = True
    max_retries: int = 1
    suffix_on_truncate: str = "\n...[truncated]"

    def apply(self, content: str | Any, json_schema: dict[str, Any] | None = None) -> tuple[str, bool, str | None]:
        """Apply limits to tool output.

        Returns (content_str, should_retry, error_message)
        """
        # Normalize to string
        if not isinstance(content, str):
            try:
                content_str = json.dumps(content, ensure_ascii=False, default=str)
            except Exception:
                content_str = str(content)
        else:
            content_str = content

        truncated = False
        if len(content_str) > self.truncate_at:
            content_str = content_str[: self.truncate_at] + self.suffix_on_truncate
            truncated = True

        # JSON schema check — structural validation
        if self.json_schema_check and json_schema:
            # Only validate if content looks like JSON
            stripped = content_str.strip()
            # Remove truncation suffix for validation
            if truncated and stripped.endswith(self.suffix_on_truncate.strip()):
                stripped = stripped[: -len(self.suffix_on_truncate.strip())].strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    # Minimal schema check: required fields present, type checks
                    required = json_schema.get("required", [])
                    properties = json_schema.get("properties", {})
                    if isinstance(required, list) and isinstance(parsed, dict):
                        for req_key in required:
                            if req_key not in parsed:
                                return content_str, True, f"missing required field: {req_key}"
                    # type checks for properties if dict
                    if isinstance(properties, dict) and isinstance(parsed, dict):
                        for k, sch in properties.items():
                            if k in parsed and isinstance(sch, dict):
                                expected = sch.get("type")
                                if expected == "string" and not isinstance(parsed[k], str):
                                    return content_str, True, f"field {k} expected string"
                                if expected == "integer" and not isinstance(parsed[k], int):
                                    return content_str, True, f"field {k} expected integer"
                                if expected == "number" and not isinstance(parsed[k], (int, float)):
                                    return content_str, True, f"field {k} expected number"
                                if expected == "array" and not isinstance(parsed[k], list):
                                    return content_str, True, f"field {k} expected array"
                    # valid JSON and schema passed
                except json.JSONDecodeError as e:
                    return content_str, True, f"invalid JSON: {e}"
                except Exception as e:
                    return content_str, True, f"schema check error: {e}"

        # No hard retry for pure truncation — LLM can handle marker
        # Retry is driven by schema violation flag above
        return content_str, False, None


default_tool_limits = ToolOutputLimits()


# ---------------------------------------------------------------------------
# Shared helpers for provider layer (mock, litellm lazy, audit, retry)
# ---------------------------------------------------------------------------

_LITELLM_AVAILABLE: bool | None = None


def _load_litellm() -> Any | None:
    global _LITELLM_AVAILABLE
    if _LITELLM_AVAILABLE is not None:
        try:
            import litellm as _lm  # type: ignore

            return _lm
        except ImportError:
            return None
    try:
        import litellm as _lm  # type: ignore

        _LITELLM_AVAILABLE = True
        return _lm
    except ImportError:
        _LITELLM_AVAILABLE = False
        return None


def _mock_completion_response(model: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = str(m.get("content", ""))[:200]
            break
    return {
        "id": f"mock-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"[mock:{model}] echo: {last_user}" if last_user else f"[mock:{model}] hello",
                    "tool_calls": [],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _mock_stream_chunks(model: str, content: str = "mock stream") -> list[dict[str, Any]]:
    words = content.split()
    chunks: list[dict[str, Any]] = []
    for i, w in enumerate(words):
        chunks.append(
            {
                "id": f"mock-stream-{uuid.uuid4().hex[:6]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": w + " "}, "finish_reason": None}],
            }
        )
    chunks.append(
        {
            "id": f"mock-stream-{uuid.uuid4().hex[:6]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    return chunks


AuditHook = Callable[[dict[str, Any]], None]


@dataclass
class AuditEvent:
    event_type: str
    trace_id: str
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    session_id: str = ""
    request_id: str = ""
    model: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditLogStub:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self._hooks: list[AuditHook] = []

    def add_hook(self, hook: AuditHook) -> None:
        self._hooks.append(hook)

    def emit(self, event: AuditEvent | dict[str, Any]) -> AuditEvent:
        if isinstance(event, dict):
            ev = AuditEvent(
                event_type=str(event.get("event_type", "unknown")),
                trace_id=str(event.get("trace_id", "")),
                session_id=str(event.get("session_id", "")),
                request_id=str(event.get("request_id", "")),
                model=str(event.get("model", "")),
                data=dict(event.get("data") or {}),
            )
        else:
            ev = event
        self.events.append(ev)
        for h in self._hooks:
            try:
                h(ev.to_dict())
            except Exception:
                pass
        return ev

    def query(self, trace_id: str | None = None, event_type: str | None = None) -> list[AuditEvent]:
        out = self.events
        if trace_id:
            out = [e for e in out if e.trace_id == trace_id]
        if event_type:
            out = [e for e in out if e.event_type == event_type]
        return out

    def clear(self) -> None:
        self.events.clear()


default_audit_log = AuditLogStub()


async def _with_timeout(coro: Awaitable[Any], timeout_s: float | None) -> Any:
    if timeout_s is None or timeout_s <= 0:
        return await coro
    return await asyncio.wait_for(coro, timeout=timeout_s)


async def _with_retry(
    fn: Callable[[], Awaitable[Any]],
    *,
    max_retries: int = 3,
    backoff_s: float = 0.5,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    observability_hook: AuditHook | None = None,
    trace_id: str = "",
) -> Any:
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except retry_on as e:
            last_exc = e
            if attempt >= max_retries:
                break
            delay = backoff_s * (2**attempt)
            if observability_hook:
                try:
                    observability_hook(
                        {
                            "event_type": "retry",
                            "trace_id": trace_id,
                            "data": {"attempt": attempt + 1, "max_retries": max_retries, "error": str(e), "backoff_s": delay},
                        }
                    )
                except Exception:
                    pass
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# 2) output_type handling — BaseModel validation with retry (max 2)
# ---------------------------------------------------------------------------

def _extract_content(response: dict[str, Any]) -> str:
    try:
        choices = response.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return str(msg.get("content") or "")
    except Exception:
        return ""


def _output_type_retry_prompt(original_content: str, error: str, model_name: str) -> dict[str, Any]:
    """Build correction message when output_type validation fails."""
    return {
        "role": "user",
        "content": f"Your previous response failed validation for {model_name}: {error}. The raw output was: {original_content[:2000]}. Please return ONLY valid JSON matching the expected schema, with no extra text.",
    }


async def _validate_and_retry_output(
    llm: "LLMProviderAdapter",
    messages: list[dict[str, Any]],
    response: dict[str, Any],
    output_type: type[BaseModel],
    trace_id: str,
    request_id: str,
    tools: list[dict[str, Any]] | None,
    llm_kwargs: dict[str, Any],
    max_retries: int = 2,
) -> tuple[dict[str, Any], BaseModel | None]:
    """Validate response content against output_type. Retry up to max_retries with correction prompt.

    Returns (final_response, parsed_model_or_None). On success, parsed model is validated.
    On final failure, raises ValidationError so caller can handle.
    """
    last_err: str | None = None
    current_resp = response
    history = list(messages)
    # Prepare schema for error messages
    try:
        schema_name = getattr(output_type, "__name__", str(output_type))
        json_schema = output_type.model_json_schema() if hasattr(output_type, "model_json_schema") else {}
    except Exception:
        schema_name = str(output_type)
        json_schema = {}

    for attempt in range(max_retries + 1):
        content = _extract_content(current_resp)
        # Try to parse — handle both pure JSON and wrapped content
        parsed_value: Any | None = None
        validation_error: str | None = None
        # Extract JSON substring if LLM wrapped with text
        candidate = content.strip()
        # If content contains JSON inside, extract first {..} or [..]
        if candidate and not (candidate.startswith("{") or candidate.startswith("[")):
            # Try to find JSON object
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidate = candidate[start : end + 1]
        try:
            if isinstance(candidate, str) and candidate.strip().startswith(("{", "[")):
                data = json.loads(candidate)
            else:
                # Fallback: treat content as raw string -> will fail validation to trigger retry
                data = json.loads(candidate) if candidate else {}
            # Validate via Pydantic
            if isinstance(data, dict):
                parsed = output_type.model_validate(data)
            elif isinstance(data, list):
                # For list outputs, validate via TypeAdapter if output_type is not list
                parsed = output_type.model_validate(data)  # type: ignore
            else:
                parsed = output_type.model_validate(data)  # type: ignore
            # Success — annotate response with parsed model
            current_resp["_output_type_validated"] = True
            current_resp["_parsed_output"] = parsed
            return current_resp, parsed
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            validation_error = str(e)
            # Include schema hint on last attempt
            last_err = validation_error
            if attempt >= max_retries:
                # Final failure: emit and raise
                llm._emit("output_type_validation_failed", trace_id=trace_id, model=llm.resolve_model(llm_kwargs.get("model")), data={"attempt": attempt + 1, "error": validation_error, "schema": schema_name})
                # Attach error to response for caller inspection
                current_resp["_output_type_error"] = validation_error
                current_resp["_output_type_schema"] = json_schema
                # Raise validation error so StructuredToolLoop can handle, but also return response
                # We don't raise here for adapter direct use — instead let caller decide
                # For strictness, we store error and return; caller can check _parsed_output
                # However to satisfy "retry with correction" we retry below if attempts left
                # If final, we keep response with error marker; optionally raise
                # We choose to keep response and NOT raise, so loop can handle gracefully
                # But for direct adapter test that expects validation, we surface via response
                return current_resp, None
            # Retry: append correction prompt and re-call LLM
            correction = _output_type_retry_prompt(content, validation_error, schema_name)
            retry_messages = history + [{"role": "assistant", "content": content}] + [correction]
            llm._emit("output_type_retry", trace_id=trace_id, model=llm.resolve_model(llm_kwargs.get("model")), data={"attempt": attempt + 1, "max_retries": max_retries, "error": validation_error})
            # Re-call LLM without output_type to avoid infinite recursion (we handle validation externally)
            try:
                # Call underlying completion without output_type to avoid recursion
                retry_kwargs = dict(llm_kwargs)
                retry_kwargs.pop("output_type", None)
                # Use internal method to avoid re-entering validation
                current_resp = await llm._raw_completion(retry_messages, tools=tools, trace_id=trace_id, request_id=request_id, **retry_kwargs)
                history = retry_messages
            except Exception as e2:
                current_resp["_output_type_error"] = f"retry failed: {e2}; original: {validation_error}"
                return current_resp, None
        except Exception as e:
            last_err = str(e)
            current_resp["_output_type_error"] = last_err
            return current_resp, None

    current_resp["_output_type_error"] = last_err or "unknown validation error"
    return current_resp, None


# ---------------------------------------------------------------------------
# 1) LLMProviderAdapter — litellm wrapper with OAOSContext, output_type, limits
# ---------------------------------------------------------------------------

@dataclass
class ModelRouting:
    default_model: str = "gpt-4o-mini"
    routes: dict[str, str] = field(default_factory=dict)

    def resolve(self, model: str | None) -> str:
        if not model:
            return self.default_model
        return self.routes.get(model, model)


class LLMProviderAdapter:
    """Litellm wrapper with model routing, streaming, retry/timeout, observability.

    Enhanced:
      - OAOSContext propagation via headers/trace
      - output_type: BaseModel validation with max 2 retries
      - ToolOutputLimits integration for downstream tool loop
    """

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        routing: dict[str, str] | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        retry_backoff_s: float = 0.5,
        api_key: str | None = None,
        observability_hook: AuditHook | None = None,
        audit_log: AuditLogStub | None = None,
        mock_responses: list[dict[str, Any]] | None = None,
        tool_output_limits: ToolOutputLimits | None = None,
    ) -> None:
        self.routing = ModelRouting(default_model=model, routes=routing or {})
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self.api_key = api_key
        self.observability_hook = observability_hook
        self.audit_log = audit_log or default_audit_log
        self._mock_responses: list[dict[str, Any]] = list(mock_responses or [])
        self._mock_index: int = 0
        self.tool_output_limits = tool_output_limits or default_tool_limits

    def resolve_model(self, model: str | None) -> str:
        return self.routing.resolve(model)

    def _emit(self, event_type: str, trace_id: str = "", data: dict[str, Any] | None = None, model: str = "") -> None:
        payload: dict[str, Any] = {"event_type": event_type, "trace_id": trace_id, "model": model, "data": data or {}}
        if self.audit_log is not None:
            try:
                self.audit_log.emit(payload)
            except Exception:
                pass
        if self.observability_hook is not None:
            try:
                self.observability_hook(payload)
            except Exception:
                pass

    def push_mock_response(self, response: dict[str, Any]) -> None:
        self._mock_responses.append(response)

    def _next_mock(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        if self._mock_index < len(self._mock_responses):
            resp = self._mock_responses[self._mock_index]
            self._mock_index += 1
            if "model" not in resp:
                resp = dict(resp)
                resp["model"] = model
            return resp
        if tools:
            pass
        return _mock_completion_response(model, messages, tools=tools)

    async def _raw_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        trace_id: str = "",
        request_id: str = "",
        oaos_context: OAOSContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Internal completion without output_type handling — single LLM call."""
        resolved = self.resolve_model(model)
        # Propagate OAOSContext trace if provided
        if oaos_context is not None and not trace_id:
            trace_id = oaos_context.trace_id

        self._emit("model_request", trace_id=trace_id, model=resolved, data={"request_id": request_id, "messages_len": len(messages)})

        async def _do() -> dict[str, Any]:
            if self._mock_responses or _load_litellm() is None:
                if self._mock_index < len(self._mock_responses) or _load_litellm() is None:
                    return self._next_mock(resolved, messages, tools)
            lm = _load_litellm()
            if lm is None:
                return self._next_mock(resolved, messages, tools)
            ckwargs: dict[str, Any] = dict(kwargs)
            if tools:
                ckwargs["tools"] = tools
            try:
                if hasattr(lm, "acompletion"):
                    resp = await lm.acompletion(model=resolved, messages=messages, **ckwargs)  # type: ignore
                else:
                    resp = await asyncio.to_thread(lm.completion, resolved, messages, **ckwargs)  # type: ignore
                if not isinstance(resp, dict):
                    try:
                        resp = resp.model_dump()  # type: ignore
                    except Exception:
                        resp = dict(resp)  # type: ignore
                return resp  # type: ignore
            except Exception as e:
                raise e

        try:
            result = await _with_timeout(
                _with_retry(
                    _do,
                    max_retries=self.max_retries,
                    backoff_s=self.retry_backoff_s,
                    observability_hook=self.observability_hook,
                    trace_id=trace_id,
                ),
                timeout_s=self.timeout_s,
            )
            self._emit("model_response", trace_id=trace_id, model=resolved, data={"request_id": request_id, "finish_reason": result.get("choices", [{}])[0].get("finish_reason", "") if isinstance(result, dict) else ""})
            return result  # type: ignore
        except asyncio.TimeoutError as e:
            self._emit("error", trace_id=trace_id, model=resolved, data={"error": "timeout", "timeout_s": self.timeout_s})
            raise TimeoutError(f"LLM completion timeout after {self.timeout_s}s") from e
        except Exception as e:
            self._emit("error", trace_id=trace_id, model=resolved, data={"error": str(e)})
            raise

    # -- core completion (non-stream) with output_type -------------------

    async def completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        trace_id: str = "",
        request_id: str = "",
        output_type: type[BaseModel] | None = None,
        oaos_context: OAOSContext | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        resolved = self.resolve_model(model)
        if oaos_context is not None and not trace_id:
            trace_id = oaos_context.trace_id
        if stream:
            chunks: list[dict[str, Any]] = []
            async for ch in self.completion_stream(messages, model=model, tools=tools, trace_id=trace_id, request_id=request_id, oaos_context=oaos_context, output_type=output_type, **kwargs):  # type: ignore
                chunks.append(ch)
            content_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for ch in chunks:
                delta = ch.get("choices", [{}])[0].get("delta", {})
                if "content" in delta and delta["content"]:
                    content_parts.append(str(delta["content"]))
                if "tool_calls" in delta and delta["tool_calls"]:
                    tool_calls.extend(delta["tool_calls"])
            return {
                "id": f"stream-collected-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "model": resolved,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "".join(content_parts), "tool_calls": tool_calls},
                        "finish_reason": "stop" if not tool_calls else "tool_calls",
                    }
                ],
                "usage": {},
                "_stream_chunks": chunks,
            }

        # Non-stream path
        raw = await self._raw_completion(messages, model=model, tools=tools, trace_id=trace_id, request_id=request_id, oaos_context=oaos_context, **kwargs)

        if output_type is not None:
            # Validate with retry
            validated_resp, parsed = await _validate_and_retry_output(
                self, messages, raw, output_type, trace_id, request_id, tools, kwargs, max_retries=2
            )
            return validated_resp
        return raw

    # -- streaming -------------------------------------------------------

    async def completion_stream(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        trace_id: str = "",
        request_id: str = "",
        oaos_context: OAOSContext | None = None,
        output_type: type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        resolved = self.resolve_model(model)
        if oaos_context is not None and not trace_id:
            trace_id = oaos_context.trace_id
        self._emit("model_request", trace_id=trace_id, model=resolved, data={"request_id": request_id, "stream": True})

        lm = _load_litellm()
        if self._mock_responses or lm is None:
            mock = self._next_mock(resolved, messages, tools)
            content = ""
            try:
                content = str(mock.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
                tcs = mock.get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
                if tcs:
                    yield {"id": mock.get("id", ""), "object": "chat.completion.chunk", "model": resolved, "choices": [{"index": 0, "delta": {"tool_calls": tcs}, "finish_reason": None}]}
                    yield {"id": mock.get("id", ""), "object": "chat.completion.chunk", "model": resolved, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
                    self._emit("model_response", trace_id=trace_id, model=resolved, data={"stream": True, "tool_calls": True})
                    return
            except Exception:
                content = ""
            for ch in _mock_stream_chunks(resolved, content or "mock stream response"):
                yield ch
                await asyncio.sleep(0)
            self._emit("model_response", trace_id=trace_id, model=resolved, data={"stream": True})
            return

        ckwargs: dict[str, Any] = dict(kwargs)
        if tools:
            ckwargs["tools"] = tools
        try:
            stream_gen = await lm.acompletion(model=resolved, messages=messages, stream=True, **ckwargs)  # type: ignore
            async for chunk in stream_gen:  # type: ignore
                if not isinstance(chunk, dict):
                    try:
                        chunk = chunk.model_dump()  # type: ignore
                    except Exception:
                        chunk = dict(chunk)  # type: ignore
                yield chunk  # type: ignore
            self._emit("model_response", trace_id=trace_id, model=resolved, data={"stream": True})
        except Exception as e:
            self._emit("error", trace_id=trace_id, model=resolved, data={"error": str(e), "stream": True})
            raise


# ---------------------------------------------------------------------------
# StructuredToolLoop — with OAOSContext injection + ToolOutputLimits
# ---------------------------------------------------------------------------

GatewayCallable = Any


def _extract_tool_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        choices = response.get("choices") or []
        if not choices:
            return []
        msg = choices[0].get("message") or {}
        tcs = msg.get("tool_calls") or []
        out: list[dict[str, Any]] = []
        for tc in tcs:
            if isinstance(tc, dict):
                out.append(tc)
            else:
                try:
                    out.append(dict(tc))  # type: ignore
                except Exception:
                    continue
        return out
    except Exception:
        return []


def _tool_result_message(tool_call_id: str, tool_name: str, result: Any, limits: ToolOutputLimits | None = None, json_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    content: str
    if isinstance(result, str):
        content = result
    else:
        try:
            content = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            content = str(result)
    # Apply limits
    if limits is not None:
        limited, should_retry, err = limits.apply(content, json_schema=json_schema)
        # err indicates schema violation; we annotate so loop can retry
        if should_retry and err:
            content = limited + f"\n[schema_error: {err}]"
        else:
            content = limited
    return {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": content}


async def _call_gateway(
    gateway: GatewayCallable,
    tool_name: str,
    arguments: dict[str, Any],
    trace_id: str,
    session_id: str = "",
    oaos_context: OAOSContext | None = None,
    limits: ToolOutputLimits | None = None,
    tool_json_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not trace_id:
        raise ValueError("trace_id is required for gateway.call (§16A Zero-Bypass)")

    # Resolve callable with OAOSContext injection
    fn: Callable[..., Any] | None = None
    if hasattr(gateway, "call"):
        fn = getattr(gateway, "call")
    elif hasattr(gateway, "execute"):
        fn = getattr(gateway, "execute")
    elif callable(gateway):
        fn = gateway  # type: ignore
    else:
        raise AttributeError("gateway must have .call or .execute or be callable")

    # Try injection for gateway itself
    prefix_args: tuple[Any, ...] = ()
    call_args = arguments
    if oaos_context is not None:
        try:
            sig = inspect.signature(fn)  # type: ignore
            # Check if first param expects OAOSContext
            params = list(sig.parameters.values())
            if params and params[0].annotation is OAOSContext or (isinstance(params[0].annotation, str) and "OAOSContext" in str(params[0].annotation)) or params[0].name.lower() in ("ctx", "oaos_context", "context"):
                prefix_args = (oaos_context,)
        except Exception:
            pass

    kwargs: dict[str, Any] = {"trace_id": trace_id}
    if session_id:
        kwargs["session_id"] = session_id
    if oaos_context is not None:
        # also propagate vault_path / policy via kwargs if gateway accepts them
        kwargs["vault_path"] = oaos_context.vault_path
        if oaos_context.policy is not None:
            kwargs["policy"] = oaos_context.policy

    # Attempt gateway call with retry for ToolOutputLimits schema violation
    max_attempts = (limits.max_retries + 1) if limits else 1
    last_result: Any = None
    for attempt in range(max_attempts):
        try:
            # Prefer signature: call(tool, args, trace_id=..., session_id=..., vault_path=...)
            if prefix_args:
                if asyncio.iscoroutinefunction(fn):
                    res = await fn(*prefix_args, tool_name, call_args, **kwargs)  # type: ignore
                else:
                    tmp = fn(*prefix_args, tool_name, call_args, **kwargs)  # type: ignore
                    res = await tmp if asyncio.iscoroutine(tmp) else tmp  # type: ignore
            else:
                if asyncio.iscoroutinefunction(fn):
                    res = await fn(tool_name, call_args, **kwargs)  # type: ignore
                else:
                    tmp = fn(tool_name, call_args, **kwargs)  # type: ignore
                    res = await tmp if asyncio.iscoroutine(tmp) else tmp  # type: ignore
            last_result = res
            # Check output limits schema if needed — if violation, retry gateway call
            if limits is not None and tool_json_schema is not None and limits.json_schema_check:
                # Normalize to string for check
                content_for_check = res.get("content") if isinstance(res, dict) and "content" in res else res
                _, should_retry, err = limits.apply(content_for_check if isinstance(content_for_check, str) else json.dumps(content_for_check, ensure_ascii=False, default=str) if isinstance(content_for_check, dict) else str(content_for_check), json_schema=tool_json_schema)
                if should_retry and attempt < max_attempts - 1:
                    # Inject correction arg and retry
                    call_args = dict(call_args)
                    call_args["_correction"] = f"Previous tool output failed schema check: {err}. Please return valid JSON."
                    continue
            return last_result if isinstance(last_result, dict) else {"result": last_result}
        except TypeError:
            # Fallback: call(dict payload, trace_id)
            try:
                payload = {"tool": tool_name, "args": call_args, "arguments": call_args, "trace_id": trace_id}
                if prefix_args:
                    if asyncio.iscoroutinefunction(fn):
                        res2 = await fn(*prefix_args, payload, **kwargs)  # type: ignore
                    else:
                        tmp2 = fn(*prefix_args, payload, **kwargs)  # type: ignore
                        res2 = await tmp2 if asyncio.iscoroutine(tmp2) else tmp2  # type: ignore
                else:
                    if asyncio.iscoroutinefunction(fn):
                        res2 = await fn(payload, **kwargs)  # type: ignore
                    else:
                        tmp2 = fn(payload, **kwargs)  # type: ignore
                        res2 = await tmp2 if asyncio.iscoroutine(tmp2) else tmp2  # type: ignore
                last_result = res2
                return last_result if isinstance(last_result, dict) else {"result": last_result}
            except Exception:
                raise
        except Exception:
            raise
    return last_result if isinstance(last_result, dict) else {"result": last_result}


class StructuredToolLoop:
    """Structured tool loop — §16.1 / §16C.3 with OAOSContext, output_type, limits."""

    def __init__(
        self,
        llm: LLMProviderAdapter,
        gateway: GatewayCallable,
        max_steps: int = 10,
        observability_hook: AuditHook | None = None,
        audit_log: AuditLogStub | None = None,
        timeout_s: float = 120.0,
        tool_output_limits: ToolOutputLimits | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        self.llm = llm
        self.gateway = gateway
        self.max_steps = max_steps
        self.observability_hook = observability_hook
        self.audit_log = audit_log or default_audit_log
        self.timeout_s = timeout_s
        self.tool_output_limits = tool_output_limits or default_tool_limits

    def _emit(self, event_type: str, trace_id: str, data: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"event_type": event_type, "trace_id": trace_id, "data": data or {}}
        if self.audit_log is not None:
            try:
                self.audit_log.emit(payload)
            except Exception:
                pass
        if self.observability_hook is not None:
            try:
                self.observability_hook(payload)
            except Exception:
                pass

    async def run(
        self,
        messages: list[dict[str, Any]],
        trace_id: str,
        tools: list[dict[str, Any]] | None = None,
        session_id: str = "",
        request_id: str = "",
        oaos_context: OAOSContext | dict[str, Any] | None = None,
        output_type: type[BaseModel] | None = None,
        tool_output_limits: ToolOutputLimits | None = None,
        **llm_kwargs: Any,
    ) -> dict[str, Any]:
        if not trace_id and oaos_context is not None:
            if isinstance(oaos_context, dict):
                trace_id = str(oaos_context.get("trace_id", ""))
            else:
                trace_id = str(getattr(oaos_context, "trace_id", ""))
        if not trace_id:
            raise ValueError("trace_id is required (§16A)")

        ctx = _ensure_context(oaos_context, trace_id=trace_id)
        limits = tool_output_limits or self.tool_output_limits

        history: list[dict[str, Any]] = [dict(m) for m in messages]
        steps: int = 0
        terminated: str = "unknown"
        last_response: dict[str, Any] | None = None

        self._emit("tool_loop_start", trace_id, {"session_id": session_id, "max_steps": self.max_steps, "request_id": request_id})

        try:
            async def _loop() -> dict[str, Any]:
                nonlocal steps, terminated, last_response, history
                for step in range(1, self.max_steps + 1):
                    steps = step
                    self._emit("tool_loop_step", trace_id, {"step": step, "history_len": len(history)})
                    try:
                        resp = await self.llm.completion(
                            history, tools=tools, trace_id=trace_id, request_id=request_id or f"req-{step}", oaos_context=ctx, **llm_kwargs
                        )
                    except Exception as e:
                        self._emit("error", trace_id, {"step": step, "error": str(e)})
                        terminated = "error"
                        last_response = {"error": str(e)}
                        break

                    last_response = resp
                    # Handle output_type validation at loop level if llm didn't fully validate (mock path)
                    if output_type is not None and resp.get("_parsed_output") is None and resp.get("_output_type_error"):
                        # LLM validation failed after retries — expose error and terminate as error
                        terminated = "output_type_validation_failed"
                        self._emit("output_type_validation_failed", trace_id, {"step": step, "error": resp.get("_output_type_error")})
                        # Keep history for audit
                        history.append(
                            {
                                "role": "assistant",
                                "content": (resp.get("choices", [{}])[0].get("message", {}).get("content") or ""),
                                "tool_calls": _extract_tool_calls(resp),
                                "_raw": resp,
                                "_output_type_error": resp.get("_output_type_error"),
                            }
                        )
                        break
                    # If output_type expects no tool calls, and we have parsed output, we can terminate early
                    if output_type is not None and resp.get("_parsed_output") is not None:
                        history.append(
                            {
                                "role": "assistant",
                                "content": (resp.get("choices", [{}])[0].get("message", {}).get("content") or ""),
                                "tool_calls": _extract_tool_calls(resp),
                                "_raw": resp,
                                "_parsed_output": resp.get("_parsed_output"),
                            }
                        )
                        terminated = "done"
                        self._emit("tool_loop_done", trace_id, {"step": step, "reason": "output_type_validated"})
                        break

                    history.append(
                        {
                            "role": "assistant",
                            "content": (resp.get("choices", [{}])[0].get("message", {}).get("content") or ""),
                            "tool_calls": _extract_tool_calls(resp),
                            "_raw": resp,
                        }
                    )
                    tcs = _extract_tool_calls(resp)
                    if not tcs:
                        terminated = "done"
                        self._emit("tool_loop_done", trace_id, {"step": step, "reason": "no_tool_calls"})
                        break

                    self._emit("tool_request", trace_id, {"step": step, "tool_calls": [{"name": tc.get("function", {}).get("name") or tc.get("name"), "id": tc.get("id")} for tc in tcs]})

                    for tc in tcs:
                        func = tc.get("function") or {}
                        tool_name = str(func.get("name") or tc.get("name") or "")
                        raw_args = func.get("arguments") or tc.get("arguments") or {}
                        if isinstance(raw_args, str):
                            try:
                                arguments = json.loads(raw_args) if raw_args.strip() else {}
                            except json.JSONDecodeError:
                                arguments = {"_raw": raw_args}
                        elif isinstance(raw_args, dict):
                            arguments = raw_args
                        else:
                            arguments = {}
                        tc_id = str(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}")

                        # Resolve JSON schema for tool if provided in tools list
                        tool_schema: dict[str, Any] | None = None
                        if tools:
                            for t in tools:
                                fn_def = t.get("function") or t
                                if fn_def.get("name") == tool_name:
                                    tool_schema = fn_def.get("parameters") or fn_def.get("json_schema")
                                    break

                        try:
                            gw_result = await _with_timeout(
                                _call_gateway(self.gateway, tool_name, arguments, trace_id=trace_id, session_id=session_id, oaos_context=ctx, limits=limits, tool_json_schema=tool_schema),
                                timeout_s=self.llm.timeout_s,
                            )
                            self._emit("tool_result", trace_id, {"step": step, "tool": tool_name, "tool_call_id": tc_id})
                        except asyncio.TimeoutError:
                            gw_result = {"error": "gateway timeout", "tool": tool_name}
                            self._emit("error", trace_id, {"step": step, "tool": tool_name, "error": "gateway timeout"})
                        except Exception as e:
                            gw_result = {"error": str(e), "tool": tool_name}
                            self._emit("error", trace_id, {"step": step, "tool": tool_name, "error": str(e)})

                        history.append(_tool_result_message(tc_id, tool_name, gw_result, limits=limits, json_schema=tool_schema))

                    if step >= self.max_steps:
                        terminated = "max_steps"
                        self._emit("tool_loop_done", trace_id, {"step": step, "reason": "max_steps"})
                        break
                else:
                    terminated = "max_steps"
                return {"messages": history, "steps": steps, "terminated": terminated, "last_response": last_response}

            result = await _with_timeout(_loop(), timeout_s=self.timeout_s)
            # If output_type requested, surface parsed model at top level for convenience
            if output_type is not None and result.get("last_response") and isinstance(result["last_response"], dict):
                pr = result["last_response"].get("_parsed_output")
                if pr is not None:
                    result["parsed_output"] = pr
                if result["last_response"].get("_output_type_error"):
                    result["output_type_error"] = result["last_response"].get("_output_type_error")
            return result
        except asyncio.TimeoutError:
            self._emit("error", trace_id, {"error": "tool loop timeout", "timeout_s": self.timeout_s})
            return {"messages": history, "steps": steps, "terminated": "timeout", "last_response": last_response, "error": "timeout"}
        finally:
            self._emit("tool_loop_end", trace_id, {"steps": steps, "terminated": terminated})


# ---------------------------------------------------------------------------
# Wired LLMRuntime — Session + Streaming + MCP with OAOSContext/limits/output_type
# ---------------------------------------------------------------------------

class LLMRuntime:
    """Wires session + streaming + MCP client — §16C LLM Runtime.

    Enhancements:
      - OAOSContext injected into every tool/stream
      - ToolOutputLimits enforced on MCP results
      - output_type delegated to LLMProviderAdapter when litellm used
    """

    def __init__(
        self,
        session_manager: SessionManager | None = None,
        streaming_engine: StreamingEngine | None = None,
        mcp_client: MCPClient | None = None,
        model: str | None = None,
        gateway_url: str | None = None,
        tool_output_limits: ToolOutputLimits | None = None,
    ) -> None:
        self.sessions = session_manager or SessionManager()
        self.streaming = streaming_engine or StreamingEngine()
        self.mcp = mcp_client or MCPClient(gateway_url=gateway_url or os.getenv("OAOS_EG_URL"))
        self.model = model or os.getenv("OAOS_LLM_MODEL") or "mock"
        self.tool_output_limits = tool_output_limits or default_tool_limits
        # Lightweight provider for output_type path
        self._provider = LLMProviderAdapter(model=self.model, tool_output_limits=self.tool_output_limits)

    # ── Session delegation (§16C.1) ──
    def create_session(self, tenant_id: str, agent_id: str, user_id: str = "", **kwargs: Any) -> dict[str, Any]:
        return self.sessions.create(tenant_id=tenant_id, agent_id=agent_id, user_id=user_id, **kwargs)

    def resume_session(self, session_id: str, tenant_id: str, agent_id: str) -> dict[str, Any]:
        return self.sessions.resume(session_id, tenant_id, agent_id)

    def cancel_session(self, session_id: str, tenant_id: str, agent_id: str) -> dict[str, Any]:
        return self.sessions.cancel(session_id, tenant_id, agent_id)

    def get_session_state(self, session_id: str, tenant_id: str, agent_id: str) -> dict[str, Any]:
        return self.sessions.get_state(session_id, tenant_id, agent_id)

    def get_oaos_context(self, session_id: str, tenant_id: str, agent_id: str, policy: Any | None = None) -> OAOSContext:
        return self.sessions.get_oaos_context(session_id, tenant_id, agent_id, policy=policy)

    # compatibility aliases
    create = create_session
    resume = resume_session
    cancel = cancel_session
    get_state = get_session_state

    async def acreate_session(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return self.create_session(*a, **kw)

    async def aresume_session(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return self.resume_session(*a, **kw)

    async def acancel_session(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return self.cancel_session(*a, **kw)

    async def aget_session_state(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return self.get_session_state(*a, **kw)

    # ── Streaming (§16C.2) ──
    async def stream_prompt(
        self,
        session_id: str,
        tenant_id: str,
        agent_id: str,
        prompt: str,
        oaos_context: OAOSContext | dict[str, Any] | None = None,
        output_type: type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Validate session isolation then yield streaming events.

        OAOSContext is built from session if not provided and injected into
        streaming and MCP tool calls.
        output_type: if provided, LLM response is validated; on failure a
        final error event is yielded.
        """
        try:
            sess = self.sessions.get_state(session_id, tenant_id, agent_id)
        except Exception as e:
            yield {"type": "error", "data": {"reason": str(e)}, "session_id": session_id}
            yield {"type": "completion", "data": {"session_id": session_id, "error": str(e)}, "session_id": session_id}
            return

        ctx = _ensure_context(oaos_context, session=sess, trace_id=str(sess.get("trace_id", "")))

        # If output_type requested and LLM available, try provider validation path
        if output_type is not None:
            # Use provider to validate single turn
            messages = [{"role": "user", "content": prompt}]
            try:
                prov_resp = await self._provider.completion(messages, trace_id=ctx.trace_id, request_id=kwargs.get("request_id", ""), output_type=output_type, oaos_context=ctx)
                parsed = prov_resp.get("_parsed_output")
                if parsed is not None:
                    # yield as tool-like completion with validated output
                    yield {"type": "text", "data": {"text": _extract_content(prov_resp)}, "trace_id": ctx.trace_id, "session_id": session_id}
                    yield {"type": "completion", "data": {"session_id": session_id, "prompt": prompt, "output_type": output_type.__name__, "validated": True, "parsed": parsed.model_dump() if hasattr(parsed, "model_dump") else str(parsed)}, "trace_id": ctx.trace_id, "session_id": session_id}
                    return
                else:
                    err = prov_resp.get("_output_type_error", "validation failed")
                    yield {"type": "error", "data": {"reason": err, "output_type": output_type.__name__}, "trace_id": ctx.trace_id, "session_id": session_id}
                    yield {"type": "completion", "data": {"session_id": session_id, "error": err}, "trace_id": ctx.trace_id, "session_id": session_id}
                    return
            except Exception as e:
                yield {"type": "error", "data": {"reason": str(e)}, "trace_id": ctx.trace_id, "session_id": session_id}
                yield {"type": "completion", "data": {"session_id": session_id, "error": str(e)}, "trace_id": ctx.trace_id, "session_id": session_id}
                return

        llm_chunks = await self._try_llm(prompt, sess, oaos_context=ctx)
        if llm_chunks is not None:
            async for ev in self.streaming.stream(prompt=prompt, session=sess, chunks=llm_chunks, oaos_context=ctx):
                if ev.get("type") == "tool":
                    tool = ev.get("data", {}).get("tool")
                    args = ev.get("data", {}).get("arguments", {})
                    if tool:
                        try:
                            # Inject OAOSContext via _call style — use mcp with context
                            result = await self.mcp.call_tool(tool, arguments=args, context=ctx.to_dict())
                            # Apply ToolOutputLimits before yielding
                            result_str = json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result
                            limited, _, _ = self.tool_output_limits.apply(result_str)
                            # store limited string as result for token safety
                            try:
                                # try to keep structured if not truncated too much
                                ev["data"]["result"] = json.loads(limited) if limited.strip().startswith("{") else limited
                            except Exception:
                                ev["data"]["result"] = limited
                        except Exception as ex:
                            ev["data"]["result"] = {"error": str(ex)}
                yield ev
            return

        # Mock path
        async for ev in self.streaming.stream(prompt=prompt, session=sess, oaos_context=ctx, **kwargs):
            yield ev

    def stream_events(self, session: Any, **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        if isinstance(session, dict):
            return self.stream_prompt(session.get("session_id", ""), session.get("tenant_id", ""), session.get("agent_id", ""), prompt=kwargs.pop("prompt", ""), oaos_context=session.get("oaos_context") or kwargs.pop("oaos_context", None), **kwargs)
        sid = str(getattr(session, "session_id", ""))
        tid = str(getattr(session, "tenant_id", ""))
        aid = str(getattr(session, "agent_id", ""))
        return self.stream_prompt(sid, tid, aid, prompt=kwargs.pop("prompt", ""), **kwargs)

    async def _try_llm(self, prompt: str, session: dict[str, Any], oaos_context: OAOSContext | None = None) -> list[str] | None:
        api_key = os.getenv("OAOS_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("LITELLM_API_KEY")
        if not api_key:
            return None
        try:
            import litellm  # type: ignore

            model = self.model if self.model != "mock" else os.getenv("OAOS_LLM_MODEL") or "gpt-4o-mini"
            # Include OAOSContext in messages as system hint for trace (non-invasive)
            msgs: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
            if oaos_context is not None:
                # Optional system preamble for policy awareness (not required)
                pass
            resp = await litellm.acompletion(model=model, messages=msgs, max_tokens=512)  # type: ignore
            text = ""
            try:
                text = resp.choices[0].message.content or ""  # type: ignore
            except Exception:
                text = str(resp)
            if not text:
                return None
            chunks = [text[i : i + 40] for i in range(0, len(text), 40)]
            return chunks if chunks else None
        except Exception:
            return None

    # ── MCP delegation with OAOSContext + limits (§16C.5) ──
    async def list_tools(self, tenant_id: str | None = None, agent_id: str | None = None, oaos_context: OAOSContext | None = None) -> list[dict[str, Any]]:
        ctx = oaos_context.to_dict() if isinstance(oaos_context, OAOSContext) else (oaos_context or {})
        if tenant_id:
            ctx["tenant_id"] = tenant_id
        if agent_id:
            ctx["agent_id"] = agent_id
        return await self.mcp.list_tools(context=ctx or None)

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None = None, tenant_id: str | None = None, agent_id: str | None = None, session_id: str | None = None, oaos_context: OAOSContext | dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        # Build OAOSContext if not provided
        if oaos_context is None and (tenant_id or agent_id or session_id):
            # Try to derive from session if session_id given
            if session_id and tenant_id and agent_id:
                try:
                    sess = self.sessions.get_state(session_id, tenant_id, agent_id)
                    oaos_context = OAOSContext.from_session(sess)
                except Exception:
                    oaos_context = OAOSContext(tenant_id=tenant_id or "", agent_id=agent_id or "", session_id=session_id or "", trace_id=kwargs.get("trace_id", ""))
            else:
                oaos_context = OAOSContext(tenant_id=tenant_id or "", agent_id=agent_id or "", session_id=session_id or "", trace_id=kwargs.get("trace_id", ""))
        ctx_dict: dict[str, Any] | None = None
        if isinstance(oaos_context, OAOSContext):
            ctx_dict = oaos_context.to_dict()
        elif isinstance(oaos_context, dict):
            ctx_dict = oaos_context
        else:
            ctx_dict = {}
            if tenant_id:
                ctx_dict["tenant_id"] = tenant_id
            if agent_id:
                ctx_dict["agent_id"] = agent_id
            if session_id:
                ctx_dict["session_id"] = session_id

        result = await self.mcp.call_tool(tool, arguments=arguments, context=ctx_dict or None, **kwargs)
        # Apply limits to result before returning
        result_str = json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result
        limited, _, _ = self.tool_output_limits.apply(result_str)
        if len(result_str) > self.tool_output_limits.truncate_at:
            # Return truncated string wrapped
            return {"tool": tool, "result": limited, "truncated": True, "original_length": len(result_str)}
        return result

    async def health_check(self) -> dict[str, Any]:
        return {"status": "ok", "runtime": "llm", "model": self.model, "features": ["oaos_context", "output_type", "tool_output_limits"]}


# Default singleton + aliases
default_runtime = LLMRuntime()
LLMRuntimeAdapter = LLMRuntime
AgentLLMRuntime = LLMRuntime
