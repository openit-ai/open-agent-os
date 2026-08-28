"""SafeRuntime — §16F.1 Dual Runtime (Safe = Default).

Spec §16F.1:
- SafeRuntime is Default runtime (LLM+MCP only, No Shell/Python).
- Allowed: create_session, send_prompt, stream_events, cancel/get_state,
           health_check, list_tools/call_tool, skills, context, model, observability.
- DENIED: execute_sandbox (shell/python) — must raise NotImplementedError.
- HermesRuntime is Advanced runtime (with Shell/Python).
- Installation options: Safe Only / Hermes Only / Both.
- Runtime access gated by Capability EXECUTE runtime/safe | runtime/hermes (JIT possible).

SafeRuntime never executes shell/python even if called via sandbox/tool.
"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator

import httpx

from .adapter import AgentRuntimeAdapter


def _sid(session: Any) -> str:
    if isinstance(session, dict):
        return str(session.get("session_id", ""))
    return str(getattr(session, "session_id", ""))


def _headers(session: Any) -> dict[str, str]:
    if isinstance(session, dict):
        return {
            "X-Tenant-Id": str(session.get("tenant_id", "")),
            "X-User-Id": str(session.get("user_id", "")),
            "X-Agent-Id": str(session.get("agent_id", "")),
            "X-Session-Id": str(session.get("session_id", "")),
            "X-Trace-Id": str(session.get("trace_id", "")),
        }
    return {
        "X-Tenant-Id": str(getattr(session, "tenant_id", "")),
        "X-User-Id": str(getattr(session, "user_id", "")),
        "X-Agent-Id": str(getattr(session, "agent_id", "")),
        "X-Session-Id": str(getattr(session, "session_id", "")),
        "X-Trace-Id": str(getattr(session, "trace_id", "")),
    }


class SafeRuntimeAdapter(AgentRuntimeAdapter):
    """Safe runtime — LLM + MCP only. Shell/Python denied per §16F.1.

    All sandbox / shell execution raises NotImplementedError with DENY reason.
    Other methods delegate to Hermes-compatible ACP endpoint when reachable,
    otherwise return local fallback so tests/offline still pass.
    """

    # Tool allowlist for Safe — LLM+MCP only
    ALLOWED_TOOL_PREFIXES: tuple[str, ...] = ("mcp:", "llm:", "model:", "")
    DENIED_LANGUAGES: set[str] = {"shell", "bash", "sh", "python", "python3", "py"}

    def __init__(self, base_url: str = "http://localhost:8001", timeout_s: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        from .skills import SkillRegistry as _SR
        from .observability import ObservabilityBus as _OB
        from .context import ContextManager as _CM

        self._skills = _SR()
        self._obs = _OB()
        self._ctx = _CM()
        self._current_model: dict[str, Any] = {"model": "safe-default", "provider": "safe"}

    # ── §16E core ────────────────────────────────────────────────────

    async def create_session(self, session: Any) -> dict[str, Any]:
        sid = _sid(session)
        payload = {"session_id": sid, "runtime": "safe"}
        url = f"{self.base_url}/acp/sessions"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                r = await c.post(url, json=payload, headers=_headers(session))
                r.raise_for_status()
                return r.json()
        except Exception as e:
            return {"status": "local_fallback", "runtime": "safe", "session_id": sid, "reason": str(e)}

    async def send_prompt(self, session: Any, prompt: str, request_id: str) -> dict[str, Any]:
        sid = _sid(session)
        url = f"{self.base_url}/acp/sessions/{sid}/prompt"
        payload = {"prompt": prompt, "request_id": request_id, "runtime": "safe"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                r = await c.post(url, json=payload, headers=_headers(session))
                r.raise_for_status()
                return r.json()
        except Exception as e:
            return {"status": "queued_local", "runtime": "safe", "request_id": request_id, "reason": str(e)}

    def stream_events(self, session: Any) -> AsyncGenerator[dict[str, Any], None]:  # type: ignore[override]
        return self._stream_events(session)

    async def _stream_events(self, session: Any) -> AsyncGenerator[dict[str, Any], None]:
        sid = _sid(session)
        url = f"{self.base_url}/acp/sessions/{sid}/stream"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                async with client.stream("GET", url, headers=_headers(session)) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(f"stream {resp.status_code}")
                    async for line in resp.aiter_lines():
                        if not line or line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            data = line[5:].strip()
                            try:
                                yield json.loads(data)
                            except json.JSONDecodeError:
                                yield {"type": "token", "data": {"text": data}}
                    return
        except Exception as e:
            yield {"type": "error", "data": {"reason": str(e), "runtime": "safe", "session_id": sid}}
            yield {"type": "done", "data": {"session_id": sid}}

    async def cancel_session(self, session_id: str, caller_user_id: str | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/acp/sessions/{session_id}/cancel"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                r = await c.post(url, json={"caller_user_id": caller_user_id})
                r.raise_for_status()
                return r.json()
        except Exception as e:
            return {"status": "cancelled_local", "runtime": "safe", "session_id": session_id, "reason": str(e)}

    async def get_session_state(self, session_id: str, caller_user_id: str | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/acp/sessions/{session_id}/state"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                r = await c.get(url, params={"caller_user_id": caller_user_id or ""})
                r.raise_for_status()
                return r.json()
        except Exception as e:
            return {"status": "unknown", "runtime": "safe", "session_id": session_id, "reason": str(e)}

    async def health_check(self) -> dict[str, Any]:
        return {"status": "ok", "runtime": "safe", "allowed": ["llm", "mcp"], "denied": ["shell", "python"]}

    # ── §16F.1 Denied: Shell/Python ──────────────────────────────────

    async def execute_sandbox(
        self,
        session: Any,
        command: str,
        language: str = "shell",
        timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        """DENY — SafeRuntime never allows shell/python (§16F.1)."""
        raise NotImplementedError("DENY: SafeRuntime does not allow shell/python execution (allowed: LLM+MCP only) §16F.1")

    # ── §16C optional — allowed in Safe ──────────────────────────────

    async def list_tools(self, session: Any | None = None) -> list[dict[str, Any]]:
        return []

    async def call_tool(
        self, session: Any, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        # Block shell/python tools explicitly even via call_tool
        low = tool_name.lower()
        if any(k in low for k in ("shell", "bash", "python", "exec", "sandbox")):
            raise NotImplementedError(f"DENY: tool '{tool_name}' not allowed in SafeRuntime §16F.1")
        return {"tool": tool_name, "arguments": arguments or {}, "result": "safe_stub", "session_id": _sid(session)}

    async def load_skill(self, skill_name: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
        m = self._skills.load(manifest or {"name": skill_name})
        return {"status": "loaded", "skill": m.to_dict()}

    async def invoke_skill(self, session: Any, skill_name: str, action: str | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._skills.invoke(skill_name, action=action, params=params, session=session)

    async def list_skills(self) -> list[dict[str, Any]]:
        return self._skills.list_dicts()

    async def unload_skill(self, skill_name: str) -> dict[str, Any]:
        ok = self._skills.unload(skill_name)
        return {"status": "unloaded" if ok else "not_found", "skill": skill_name}

    async def get_context(self, session_id: str) -> dict[str, Any]:
        w = self._ctx.get(session_id)
        if w is None:
            return {"session_id": session_id, "messages": [], "usage": {"tokens": 0}}
        return {"session_id": session_id, "messages": w.get(), "usage": w.usage()}

    async def update_context(self, session_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        w = self._ctx.update(session_id, messages)
        return {"session_id": session_id, "messages": w.get(), "usage": w.usage()}

    async def compact_context(self, session_id: str, max_tokens: int | None = None) -> dict[str, Any]:
        w = self._ctx.get(session_id)
        if w is None:
            return {"compacted": False, "reason": "no_context"}
        return await w.compact(max_tokens=max_tokens)

    async def get_context_usage(self, session_id: str) -> dict[str, Any]:
        w = self._ctx.get(session_id)
        if w is None:
            return {"session_id": session_id, "tokens": 0, "messages": 0}
        return w.usage()

    async def set_model(self, session: Any, model: str, provider: str | None = None) -> dict[str, Any]:
        self._current_model = {"model": model, "provider": provider or "safe"}
        return {"status": "ok", **self._current_model, "session_id": _sid(session)}

    async def get_model(self, session: Any | None = None) -> dict[str, Any]:
        return dict(self._current_model)

    async def list_models(self) -> list[dict[str, Any]]:
        return [dict(self._current_model)]

    async def emit_event(self, event: dict[str, Any]) -> dict[str, Any]:
        from .observability import RuntimeEvent

        rt = RuntimeEvent(
            event_type=str(event.get("event_type") or event.get("type") or "unknown"),
            trace_id=str(event.get("trace_id", "")),
            session_id=str(event.get("session_id", "")),
            request_id=str(event.get("request_id", "")),
            agent_id=str(event.get("agent_id", "")),
            user_id=str(event.get("user_id", "")),
            data={k: v for k, v in event.items() if k not in ("event_type", "type", "trace_id", "session_id", "request_id", "agent_id", "user_id")},
        )
        self._obs.emit(rt)
        return {"status": "ok", "event": rt.to_dict()}


# Aliases — v1.5 §16E.6: SafeRuntime is deprecated, LLM Runtime is canonical
SafeRuntime = SafeRuntimeAdapter
LLMRuntime = SafeRuntimeAdapter
LLMRuntimeAdapter = SafeRuntimeAdapter
