"""Risk Classification — Section 21"""
from enum import Enum

class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

# Deterministic mapping — never LLM-based (Section 45)
HIGH_RISK_ACTIONS = {"SEND", "DELETE", "DEPLOY", "MERGE", "PAY", "EXPORT", "SHARE", "ADMIN"}
MEDIUM_RISK_ACTIONS = {"CREATE", "MODIFY", "EXECUTE"}

def classify(action: str, resource: str, is_external: bool = False) -> RiskLevel:
    if action in HIGH_RISK_ACTIONS or is_external or "bulk" in resource or "export" in resource.lower():
        return RiskLevel.HIGH
    if action in MEDIUM_RISK_ACTIONS:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW
