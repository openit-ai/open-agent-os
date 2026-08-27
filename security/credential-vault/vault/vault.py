"""Personal Credential Vault — Section 10. Encrypted, never plaintext, never in Hermes process.
- AES-GCM encrypt stub with cryptography.fernet (AES-128-CBC + HMAC-SHA256, AES-GCM 동등 보안 수준)
- owner check (agent_id must match delegation)
- PERSONAL_CREDENTIAL_USE audit event 기록
- DB persistence: when `session_maker` (async_sessionmaker) is provided, vault_credentials table is used;
  otherwise in-memory dict (backward-compatible for tests/dev).
"""
from __future__ import annotations

import base64
import hashlib
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


def _derive_fernet_key(raw_key: bytes) -> bytes:
    """raw bytes → Fernet 32-byte urlsafe key (sha256 → 32bytes → b64)."""
    digest = hashlib.sha256(raw_key).digest()  # 32 bytes
    return base64.urlsafe_b64encode(digest)


class CredentialVault(ABC):
    @abstractmethod
    async def store(self, user_id: str, provider: str, scope: str, token: bytes) -> str:
        """Encrypt and store, return secret_ref"""
        ...

    @abstractmethod
    async def retrieve(self, secret_ref: str, requester_agent_id: str) -> bytes:
        """Decrypt — caller must be owner agent, logged as PERSONAL_CREDENTIAL_USE"""
        ...

    @abstractmethod
    async def revoke(self, secret_ref: str) -> None:
        ...


class EncryptedPostgresVault(CredentialVault):
    """Fernet(AES-128-CBC + HMAC) encrypted column (Section 10.2).

    Dual-mode:
      - In-memory dict (default): `session_maker is None` → existing behavior, tests pass without DB.
      - DB-backed: provide `session_maker` (async_sessionmaker) or `db_url` → vault_credentials table.
    Owner check + PERSONAL_CREDENTIAL_USE audit event in both modes.
    """

    def __init__(
        self,
        encryption_key: bytes,
        delegation_service=None,
        audit_ledger=None,
        session_maker=None,
        db_url: str | None = None,
    ) -> None:
        fernet_key = _derive_fernet_key(encryption_key)
        self._fernet = Fernet(fernet_key)
        self.key = encryption_key
        # in-memory fallback stores
        self._store: dict[str, bytes] = {}
        self._meta: dict[str, dict] = {}
        self._delegation_service = delegation_service
        self._audit_ledger = audit_ledger
        self._audit_events: list[dict] = []
        # DB wiring
        self._session_maker = session_maker
        if self._session_maker is None and db_url:
            try:
                from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

                _url = db_url
                if _url.startswith("postgresql://"):
                    _url = _url.replace("postgresql://", "postgresql+asyncpg://", 1)
                _engine = create_async_engine(_url, pool_pre_ping=True)
                self._session_maker = async_sessionmaker(_engine, expire_on_commit=False)
            except Exception:
                self._session_maker = None

    def set_session_maker(self, session_maker) -> None:
        """Inject/replace DB session maker at runtime (e.g. from FastAPI lifespan)."""
        self._session_maker = session_maker

    # ── internal DB helpers (lazy import to avoid hard dep in tests) ─────
    async def _db_insert(self, secret_ref: str, user_id: str, owner_agent_id: str, provider: str, scope: str, encrypted: bytes) -> None:
        from security.models.orm import VaultCredentialORM  # lazy

        async with self._session_maker() as sess:  # type: ignore
            row = VaultCredentialORM(
                secret_ref=secret_ref,
                user_id=user_id,
                owner_agent_id=owner_agent_id,
                provider=provider,
                scope=scope,
                encrypted_token=encrypted,
                created_at=datetime.now(timezone.utc),
            )
            sess.add(row)
            await sess.commit()

    async def _db_get(self, secret_ref: str):
        from sqlalchemy import select
        from security.models.orm import VaultCredentialORM

        async with self._session_maker() as sess:  # type: ignore
            res = await sess.execute(select(VaultCredentialORM).where(VaultCredentialORM.secret_ref == secret_ref))
            return res.scalar_one_or_none()

    async def _db_delete(self, secret_ref: str) -> None:
        from sqlalchemy import delete
        from security.models.orm import VaultCredentialORM

        async with self._session_maker() as sess:  # type: ignore
            await sess.execute(delete(VaultCredentialORM).where(VaultCredentialORM.secret_ref == secret_ref))
            await sess.commit()

    async def store(self, user_id: str, provider: str, scope: str, token: bytes) -> str:
        ref = f"secret_{uuid.uuid4().hex[:12]}"
        suffix = user_id.split(":")[-1] if ":" in user_id else user_id
        owner_agent_id = f"agent:assistant:{suffix}"
        encrypted = self._fernet.encrypt(token)

        if self._session_maker is not None:
            try:
                await self._db_insert(ref, user_id, owner_agent_id, provider, scope, encrypted)
                # also keep meta for owner_of fallback
                self._meta[ref] = {
                    "user_id": user_id,
                    "owner_agent_id": owner_agent_id,
                    "provider": provider,
                    "scope": scope,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                return ref
            except Exception:
                # fall through to memory on DB failure
                pass

        self._store[ref] = encrypted
        self._meta[ref] = {
            "user_id": user_id,
            "owner_agent_id": owner_agent_id,
            "provider": provider,
            "scope": scope,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return ref

    async def retrieve(self, secret_ref: str, requester_agent_id: str) -> bytes:
        # fetch encrypted + meta (DB preferred)
        encrypted: bytes | None = None
        meta: dict | None = None

        if self._session_maker is not None:
            try:
                row = await self._db_get(secret_ref)
                if row is not None:
                    encrypted = row.encrypted_token
                    meta = {
                        "user_id": row.user_id,
                        "owner_agent_id": row.owner_agent_id,
                        "provider": row.provider,
                        "scope": row.scope,
                    }
                else:
                    # fall back to memory if not in DB
                    encrypted = self._store.get(secret_ref)
                    meta = self._meta.get(secret_ref)
            except Exception:
                encrypted = self._store.get(secret_ref)
                meta = self._meta.get(secret_ref)
        else:
            encrypted = self._store.get(secret_ref)
            meta = self._meta.get(secret_ref)

        if encrypted is None:
            raise KeyError(f"secret not found: {secret_ref}")

        owner = (meta or {}).get("owner_agent_id")
        if owner and requester_agent_id != owner:
            raise PermissionError(f"credential isolation violation: owner={owner} requester={requester_agent_id}")

        try:
            plaintext = self._fernet.decrypt(encrypted)
        except InvalidToken as e:
            raise ValueError("decryption failed — invalid key or corrupted token") from e

        event = {
            "event_type": "PERSONAL_CREDENTIAL_USE",
            "secret_ref": secret_ref,
            "requester_agent_id": requester_agent_id,
            "provider": (meta or {}).get("provider"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._audit_events.append(event)

        if self._audit_ledger is not None:
            try:
                from audit_model import AuditEvent, AuditEventType

                ae = AuditEvent(
                    event_id=f"evt_{uuid.uuid4().hex[:12]}",
                    event_type=AuditEventType.PERSONAL_CREDENTIAL_USE,
                    timestamp=datetime.now(timezone.utc),
                    tenant_id="default",
                    user_id=(meta or {}).get("user_id"),
                    agent_id=requester_agent_id,
                    resource=secret_ref,
                    action="RETRIEVE",
                )
                self._audit_ledger.append(ae)
            except Exception:
                pass

        return plaintext

    async def revoke(self, secret_ref: str) -> None:
        if self._session_maker is not None:
            try:
                await self._db_delete(secret_ref)
            except Exception:
                pass
        self._store.pop(secret_ref, None)
        self._meta.pop(secret_ref, None)

    def audit_events(self) -> list[dict]:
        return list(self._audit_events)

    def owner_of(self, secret_ref: str) -> str | None:
        return self._meta.get(secret_ref, {}).get("owner_agent_id")
