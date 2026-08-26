"""Session Router — selects Hermes Security Domain Worker Pool (Section 16)"""
from .session import SessionRecord

DOMAIN_POOLS = {
    "general": "hermes-general",
    "development": "hermes-dev",
    "finance_hr": "hermes-finance-hr",
    "admin": "hermes-admin",
    "high_risk_ephemeral": "hermes-ephemeral",
}

def select_worker_pool(security_domain: str, risk_level: str = "LOW") -> str:
    if risk_level == "HIGH":
        return DOMAIN_POOLS["high_risk_ephemeral"]
    return DOMAIN_POOLS.get(security_domain, DOMAIN_POOLS["general"])
