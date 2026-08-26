"""iam adapter stub — implement MCP tools/resources for iam."""
class IamAdapter:
    name = "iam"
    async def list_tools(self) -> list[str]:
        return []
    async def list_resources(self) -> list[str]:
        return []
