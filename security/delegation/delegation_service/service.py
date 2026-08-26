"""Delegation Service — Section 9. User → Agent consent."""
from datetime import datetime, timezone
import uuid
from delegation_model import Delegation, DelegationStatus

class DelegationService:
    def __init__(self):
        self._store: dict[str, Delegation] = {}

    def grant(self, user_id: str, agent_id: str, provider: str, scope: str) -> Delegation:
        d = Delegation(
            id=f"dlg_{uuid.uuid4().hex[:12]}",
            user_id=user_id, agent_id=agent_id, provider=provider, scope=scope,
            status=DelegationStatus.ACTIVE, created_at=datetime.now(timezone.utc),
        )
        self._store[d.id] = d
        return d

    def revoke(self, delegation_id: str) -> None:
        if d := self._store.get(delegation_id):
            d.status = DelegationStatus.REVOKED
            d.revoked_at = datetime.now(timezone.utc)

    def is_active(self, delegation_id: str) -> bool:
        d = self._store.get(delegation_id)
        return d is not None and d.status == DelegationStatus.ACTIVE
