"""hermes adapter stub — implement MCP tools/resources for hermes."""
class HermesAdapter:
    name = "hermes"
    async def list_tools(self) -> list[str]:
        return []
    async def list_resources(self) -> list[str]:
        return []
