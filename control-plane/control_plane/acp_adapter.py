"""ACP Adapter — Internal Agent Interface ↔ Hermes ACP (Section 17)"""
from typing import AsyncGenerator
from agent_context import AgentContext

class ACPAdapter:
    """Translates InternalAgentInterface calls to Hermes ACP wire format.
    Hermes core is NOT modified; this is the only integration point (Section 17).
    """
    def __init__(self, hermes_base_url: str):
        self.hermes_base_url = hermes_base_url

    async def create_session(self, ctx: AgentContext) -> str:
        # TODO: POST {hermes_base_url}/acp/sessions
        raise NotImplementedError

    async def send_prompt(self, ctx: AgentContext, prompt: str) -> None:
        raise NotImplementedError

    async def stream_events(self, ctx: AgentContext) -> AsyncGenerator[dict, None]:
        # SSE / WebSocket from Hermes
        if False:
            yield {}
        raise NotImplementedError
