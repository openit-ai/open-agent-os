"""Capability Enforcement — verifies Capability Token before proxying to MCP (Section 20, 26)"""
from dataclasses import dataclass

@dataclass
class CapabilityCheck:
    allowed: bool
    reason: str
    token_id: str | None = None

def verify_capability(token: dict, action: str, resource: str) -> CapabilityCheck:
    # Real impl: verify JWT signature, expiry, nonce, resource match, delegation binding
    # Stub keeps the interface stable for Phase 1
    if token.get("action") != action:
        return CapabilityCheck(False, "action mismatch")
    if token.get("resource") != resource and not resource.startswith(token.get("resource","").rstrip("*")):
        return CapabilityCheck(False, "resource mismatch")
    return CapabilityCheck(True, "ok", token.get("jti"))
