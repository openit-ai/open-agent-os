"""Privileged Tool Proxy — all HIGH-risk tool calls go through here (Section 7.2)"""
from .capability import verify_capability
from .risk import classify

async def proxy_tool_call(tool_name: str, args: dict, capability_token: dict | None, context: dict) -> dict:
    action = context.get("action", "EXECUTE")
    resource = context.get("resource", tool_name)
    risk = classify(action, resource, is_external=context.get("is_external", False))
    if risk.value == "HIGH" and not capability_token:
        return {"error": "CAPABILITY_REQUIRED", "risk": risk.value}
    if capability_token:
        check = verify_capability(capability_token, action, resource)
        if not check.allowed:
            return {"error": "CAPABILITY_DENIED", "reason": check.reason}
    # TODO: forward to MCP server
    return {"ok": True, "risk": risk.value, "tool": tool_name}
