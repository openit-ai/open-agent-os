"""Runtime Adapter Contract — Section 16E.

Defines the abstract interface that all runtime backends (Hermes, future runtimes)
must implement. Control-plane and execution-gateway depend only on this contract.
"""

from __future__ import annotations

import abc
from typing import Any, AsyncGenerator, TYPE_CHECKING

if TYPE_CHECKING:
    # Avoid circular import at runtime; SessionRecord lives in control-plane
    from control_plane.session import SessionRecord  # type: ignore[import-untyped]  # pragma: no cover


class AgentRuntimeAdapter(abc.ABC):
    """Abstract runtime adapter — §16E Runtime-Agnostic contract.

    Every concrete adapter must implement the six operations below.
    Signatures are intentionally compatible with both the legacy
    ``control_plane.hermes_adapter.HermesAdapter`` / ``acp_adapter.ACPAdapter``
    and the new contract so migration is non-breaking.
    """

    @abc.abstractmethod
    async def create_session(self, session: Any) -> dict[str, Any]:
        """Create a session on the runtime side.

        Args:
            session: SessionRecord (or compatible dict) containing tenant/user/agent/session/trace.

        Returns:
            Dict with at least ``session_id`` and ``status`` keys.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def send_prompt(self, session: Any, prompt: str, request_id: str) -> dict[str, Any]:
        """Send a prompt to the runtime for the given session."""
        raise NotImplementedError

    @abc.abstractmethod
    def stream_events(self, session: Any) -> AsyncGenerator[dict[str, Any], None]:
        """Stream runtime events (token / tool_call / approval_request / done / error).

        Must be an async generator yielding dicts.
        """
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
        """Return runtime health status.

        Returns:
            Dict with at least ``status`` (``ok`` / ``degraded`` / ``error``).
        """
        raise NotImplementedError
