"""Runtime Adapter Contract — Section 16E + 16C (10 requirements).

Defines the abstract interface that all runtime backends (Hermes, future runtimes)
must implement. Control-plane and execution-gateway depend only on this contract.

- 6 core abstract methods (§16E) — must be implemented.
- §16C optional contracts (Reasoning/Skill/Model/Observability/Context/Tool/MCP/Sandbox)
  — default impl raises NotImplementedError or returns safe fallback, so existing
  HermesAdapter stays compatible.
"""

from __future__ import annotations

import abc
from typing import Any, AsyncGenerator, TYPE_CHECKING

if TYPE_CHECKING:
    from control_plane.session import SessionRecord  # type: ignore[import-untyped]  # pragma: no cover


class AgentRuntimeAdapter(abc.ABC):
    """Abstract runtime adapter — §16E + §16C common requirements.

    Core 6 (§16E) are abstract. §16C extensions have default impl.
    """

    # ── §16E Core (abstract) ────────────────────────────────────────────

    @abc.abstractmethod
    async def create_session(self, session: Any) -> dict[str, Any]:
        """Create a session on the runtime side."""
        raise NotImplementedError

    @abc.abstractmethod
    async def send_prompt(self, session: Any, prompt: str, request_id: str) -> dict[str, Any]:
        """Send a prompt to the runtime for the given session."""
        raise NotImplementedError

    @abc.abstractmethod
    def stream_events(self, session: Any) -> AsyncGenerator[dict[str, Any], None]:
        """Stream runtime events (token / tool_call / approval_request / done / error)."""
        raise NotImplementedError  # type: ignore[return]
        yield  # make this an async generator for type-checkers
        assert False, "unreachable"

    @abc.abstractmethod
    async def cancel_session(self, session_id: str, caller_user_id: str | None = None) -> dict[str, Any]:
        """Cancel an active session."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_session_state(self, session_id: str, caller_user_id: str | None = None) -> dict[str, Any]:
        """Return current session state from the runtime."""
        raise NotImplementedError

    @abc.abstractmethod
    async def health_check(self) -> dict[str, Any]:
        """Return runtime health status."""
        raise NotImplementedError

    # ── §16C.3 Reasoning Loop (optional) ────────────────────────────────

    async def reasoning_step(
        self,
        session: Any,
        step_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Single think→act→observe step (§16C.3). Default: not implemented."""
        raise NotImplementedError("reasoning_step not implemented for this runtime")

    async def loop_until(
        self,
        session: Any,
        *,
        max_steps: int = 20,
        done_fn: Any | None = None,
    ) -> dict[str, Any]:
        """Iterative reasoning loop until done or max_steps (§16C.3)."""
        raise NotImplementedError("loop_until not implemented for this runtime")

    # ── §16C.4 Tool Calling + §16C.5 MCP (optional) ────────────────────

    async def list_tools(self, session: Any | None = None) -> list[dict[str, Any]]:
        """List available tools (MCP discovery, §16C.4/§16C.5)."""
        return []

    async def call_tool(
        self,
        session: Any,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke a tool via runtime (§16C.4)."""
        raise NotImplementedError("call_tool not implemented for this runtime")

    # ── §16C.6 Skill / Extension (optional) ─────────────────────────────

    async def load_skill(self, skill_name: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
        """Load a skill/plugin (§16C.6)."""
        raise NotImplementedError("load_skill not implemented for this runtime")

    async def invoke_skill(
        self,
        session: Any,
        skill_name: str,
        action: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke a loaded skill (§16C.6)."""
        raise NotImplementedError("invoke_skill not implemented for this runtime")

    async def list_skills(self) -> list[dict[str, Any]]:
        """List loaded skills."""
        return []

    async def unload_skill(self, skill_name: str) -> dict[str, Any]:
        """Unload a skill."""
        raise NotImplementedError("unload_skill not implemented for this runtime")

    # ── §16C.7 Sandbox (optional) ───────────────────────────────────────

    async def execute_sandbox(
        self,
        session: Any,
        command: str,
        language: str = "shell",
        timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        """Execute code in local sandbox (§16C.7)."""
        raise NotImplementedError("execute_sandbox not implemented for this runtime")

    # ── §16C.8 Context Management (optional) ────────────────────────────

    async def get_context(self, session_id: str) -> dict[str, Any]:
        """Get conversation context window (§16C.8)."""
        raise NotImplementedError("get_context not implemented for this runtime")

    async def update_context(self, session_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Update/append context window (§16C.8)."""
        raise NotImplementedError("update_context not implemented for this runtime")

    async def compact_context(self, session_id: str, max_tokens: int | None = None) -> dict[str, Any]:
        """Compact context window (§16C.8)."""
        raise NotImplementedError("compact_context not implemented for this runtime")

    async def get_context_usage(self, session_id: str) -> dict[str, Any]:
        """Return context usage stats (tokens, window)."""
        return {"session_id": session_id, "usage": "unknown"}

    # ── §16C.9 Model Provider (optional) ────────────────────────────────

    async def set_model(self, session: Any, model: str, provider: str | None = None) -> dict[str, Any]:
        """Select model/provider per session or globally (§16C.9)."""
        raise NotImplementedError("set_model not implemented for this runtime")

    async def get_model(self, session: Any | None = None) -> dict[str, Any]:
        """Get current model/provider (§16C.9)."""
        return {"model": "default", "provider": "unknown"}

    async def list_models(self) -> list[dict[str, Any]]:
        """List available models/providers."""
        return []

    # ── §16C.10 Observability (optional) ────────────────────────────────

    async def emit_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Emit structured observability event (§16C.10)."""
        # default: no-op echo
        return {"status": "ok", "event": event}

    def get_trace_context(self, session: Any) -> dict[str, str]:
        """Extract trace/session propagation headers (§16C.10)."""
        if isinstance(session, dict):
            return {
                "trace_id": str(session.get("trace_id", "")),
                "session_id": str(session.get("session_id", "")),
                "request_id": str(session.get("request_id", "")),
                "agent_id": str(session.get("agent_id", "")),
                "user_id": str(session.get("user_id", "")),
            }
        return {
            "trace_id": str(getattr(session, "trace_id", "")),
            "session_id": str(getattr(session, "session_id", "")),
            "request_id": str(getattr(session, "request_id", "")),
            "agent_id": str(getattr(session, "agent_id", "")),
            "user_id": str(getattr(session, "user_id", "")),
        }

    # ── Convenience aliases (back-compat with §16E draft names) ─────────

    async def resume_session(self, session: Any) -> dict[str, Any]:
        """Resume an existing session (alias for create_session with resume flag)."""
        return await self.create_session(session)

    async def shutdown_session(self, session_id: str) -> dict[str, Any]:
        """Shutdown alias for cancel_session."""
        return await self.cancel_session(session_id)
