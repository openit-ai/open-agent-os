"""Delegation Service — Section 9. User → Agent consent.
- grant/revoke with status
- in-memory + secure hash (sha256 delegation fingerprint)
- revoke 시 credential binding 무효 (cascade)
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from delegation_model import CredentialBinding, CredentialBindingStatus, Delegation, DelegationStatus


def _delegation_hash(d: Delegation) -> str:
    """위임 fingerprint — 변조 탐지용 secure hash."""
    raw = f"{d.id}|{d.user_id}|{d.agent_id}|{d.provider}|{d.scope}|{d.status.value}"
    return hashlib.sha256(raw.encode()).hexdigest()


class DelegationService:
    """In-memory delegation & credential binding store.

    - grant: Delegation 생성 + secure hash 기록
    - bind_credential: CredentialBinding 생성 (secret_ref 연결)
    - revoke: Delegation REVOKED + 연결된 모든 CredentialBinding REVOKED
    """

    def __init__(self) -> None:
        self._store: dict[str, Delegation] = {}
        self._hash_store: dict[str, str] = {}  # delegation_id → sha256 hash
        self._bindings: dict[str, CredentialBinding] = {}  # binding_id → binding
        self._delegation_bindings: dict[str, set[str]] = {}  # delegation_id → set(binding_id)

    # ── Delegation ──────────────────────────────────────────────
    def grant(
        self,
        user_id: str,
        agent_id: str,
        provider: str,
        scope: str,
        expires_at: datetime | None = None,
    ) -> Delegation:
        d = Delegation(
            id=f"dlg_{uuid.uuid4().hex[:12]}",
            user_id=user_id,
            agent_id=agent_id,
            provider=provider,
            scope=scope,
            status=DelegationStatus.ACTIVE,
            created_at=datetime.now(timezone.utc),
            expires_at=expires_at,
        )
        self._store[d.id] = d
        self._hash_store[d.id] = _delegation_hash(d)
        self._delegation_bindings[d.id] = set()
        return d

    def revoke(self, delegation_id: str) -> Delegation | None:
        """Revoke delegation + cascade to credential bindings."""
        d = self._store.get(delegation_id)
        if d is None:
            return None
        if d.status == DelegationStatus.REVOKED:
            return d
        d.status = DelegationStatus.REVOKED
        d.revoked_at = datetime.now(timezone.utc)
        self._hash_store[d.id] = _delegation_hash(d)
        # cascade: 모든 연결된 binding 무효화
        for bid in self._delegation_bindings.get(delegation_id, set()):
            b = self._bindings.get(bid)
            if b and b.status == CredentialBindingStatus.ACTIVE:
                b.status = CredentialBindingStatus.REVOKED
        return d

    def is_active(self, delegation_id: str) -> bool:
        d = self._store.get(delegation_id)
        if d is None:
            return False
        if d.status != DelegationStatus.ACTIVE:
            return False
        if d.expires_at and d.expires_at < datetime.now(timezone.utc):
            return False
        return True

    def get(self, delegation_id: str) -> Delegation | None:
        return self._store.get(delegation_id)

    def verify_hash(self, delegation_id: str) -> bool:
        """저장된 hash 와 현재 delegation 상태 hash 비교 — 변조 탐지."""
        d = self._store.get(delegation_id)
        if d is None:
            return False
        return self._hash_store.get(d.id) == _delegation_hash(d)

    def list_by_user(self, user_id: str) -> list[Delegation]:
        return [d for d in self._store.values() if d.user_id == user_id]

    # ── CredentialBinding ───────────────────────────────────────
    def bind_credential(
        self,
        delegation_id: str,
        provider: str,
        secret_ref: str,
        scope: str,
        expires_at: datetime | None = None,
    ) -> CredentialBinding:
        d = self._store.get(delegation_id)
        if d is None:
            raise ValueError(f"delegation not found: {delegation_id}")
        if d.status != DelegationStatus.ACTIVE:
            raise ValueError(f"delegation not active: {delegation_id}")
        b = CredentialBinding(
            id=f"cred_{uuid.uuid4().hex[:12]}",
            delegation_id=delegation_id,
            provider=provider,
            secret_ref=secret_ref,
            scope=scope,
            status=CredentialBindingStatus.ACTIVE,
            expires_at=expires_at,
        )
        self._bindings[b.id] = b
        self._delegation_bindings.setdefault(delegation_id, set()).add(b.id)
        return b

    def get_binding(self, binding_id: str) -> CredentialBinding | None:
        return self._bindings.get(binding_id)

    def is_binding_active(self, binding_id: str) -> bool:
        b = self._bindings.get(binding_id)
        if b is None:
            return False
        if b.status != CredentialBindingStatus.ACTIVE:
            return False
        # 부모 delegation 도 active 여야 함
        if not self.is_active(b.delegation_id):
            return False
        if b.expires_at and b.expires_at < datetime.now(timezone.utc):
            return False
        return True

    def revoke_binding(self, binding_id: str) -> None:
        b = self._bindings.get(binding_id)
        if b:
            b.status = CredentialBindingStatus.REVOKED
