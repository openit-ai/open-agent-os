"""microsoft adapter stub — implement MCP tools/resources for microsoft."""
class MicrosoftAdapter:
    name = "microsoft"
    async def list_tools(self) -> list[str]:
        return []
    async def list_resources(self) -> list[str]:
        return []
