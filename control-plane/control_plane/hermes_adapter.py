"""Hermes Runtime Adapter — Security Domain Worker Pool routing (Section 16)."""
from __future__ import annotations
from .router import select_worker_pool
from .session import SessionRecord

class HermesAdapter:
    def __init__(self, hermes_base_url: str):
        self.hermes_base_url = hermes_base_url

    def resolve_pool(self, session: SessionRecord, action: str | None = None, risk: str = "LOW") -> str:
        return select_worker_pool(session.security_domain, risk_level=risk, action=action)

    def worker_url(self, pool: str) -> str:
        # In prod: pool maps to K8s service or VM; in dev: single Hermes instance
        return self.hermes_base_url
