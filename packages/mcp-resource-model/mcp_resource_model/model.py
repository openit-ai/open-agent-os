"""MCP Resource Model — Section 35. Tool discovery normalization."""
from __future__ import annotations
from pydantic import BaseModel, Field

class MCPResource(BaseModel):
    uri: str  # e.g. gmail://user/kim/messages
    name: str
    description: str | None = None
    mime_type: str | None = None

class MCPTool(BaseModel):
    name: str
    description: str
    input_schema: dict
    resource: str  # canonical resource this tool touches
    action: str
    risk_level: str = "MEDIUM"
    requires_capability: bool = True
