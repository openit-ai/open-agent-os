"""Session Router — selects Hermes Security Domain Worker Pool (Section 16).

Never shared pool for high-risk / finance_hr / admin.
"""
from typing import Literal

DOMAIN_POOLS: dict[str, str] = {
    "general": "hermes-general",
    "development": "hermes-dev",
    "finance_hr": "hermes-finance-hr",
    "admin": "hermes-admin",
    "high_risk_ephemeral": "hermes-ephemeral",
}

HIGH_RISK_ACTIONS = {"DEPLOY", "MERGE", "PAY", "DELETE", "EXPORT"}

def select_worker_pool(security_domain: str, risk_level: str = "LOW", action: str | None = None) -> str:
    if action in HIGH_RISK_ACTIONS or risk_level == "HIGH":
        return DOMAIN_POOLS["high_risk_ephemeral"]
    return DOMAIN_POOLS.get(security_domain, DOMAIN_POOLS["general"])

def route_session(security_domain: str, action: str | None = None) -> dict:
    pool = select_worker_pool(security_domain, action=action or "")
    return {"pool": pool, "security_domain": security_domain}
