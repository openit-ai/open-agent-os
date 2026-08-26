"""slack adapter stub — implement MCP tools/resources for slack."""
class SlackAdapter:
    name = "slack"
    async def list_tools(self) -> list[str]:
        return []
    async def list_resources(self) -> list[str]:
        return []
