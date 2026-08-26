"""Hash-chain Audit Ledger — Section 31."""
import hashlib, json
from audit_model import AuditEvent

class AuditLedger:
    def __init__(self):
        self._head: str | None = None
        self._events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> AuditEvent:
        event.previous_hash = self._head
        event.event_hash = event.compute_hash()
        self._head = event.event_hash
        self._events.append(event)
        return event

    @property
    def head(self) -> str | None:
        return self._head

    def verify_chain(self) -> bool:
        prev = None
        for e in self._events:
            expected_prev = prev
            if e.previous_hash != expected_prev:
                return False
            if e.compute_hash() != e.event_hash:
                return False
            prev = e.event_hash
        return True
