"""mattermost adapter stub — implement MCP tools/resources for mattermost."""
class MattermostAdapter:
    name = "mattermost"
    async def list_tools(self) -> list[str]:
        return []
    async def list_resources(self) -> list[str]:
        return []
