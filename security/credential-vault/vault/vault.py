"""Personal Credential Vault — Section 10. Encrypted, never plaintext, never in Hermes process."""
from abc import ABC, abstractmethod

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
    async def revoke(self, secret_ref: str) -> None: ...

class EncryptedPostgresVault(CredentialVault):
    """Reference impl: AES-GCM encrypted column in Postgres (Section 10.2)"""
    def __init__(self, encryption_key: bytes):
        self.key = encryption_key  # placeholder to keep import valid
        self._store: dict[str, bytes] = {}  # replace with real DB

    async def store(self, user_id: str, provider: str, scope: str, token: bytes) -> str:
        import uuid
        ref = f"secret_{uuid.uuid4().hex[:12]}"
        self._store[ref] = token  # TODO: encrypt with self.key
        return ref

    async def retrieve(self, secret_ref: str, requester_agent_id: str) -> bytes:
        # TODO: verify requester_agent_id owns the delegation for secret_ref
        return self._store[secret_ref]

    async def revoke(self, secret_ref: str) -> None:
        self._store.pop(secret_ref, None)
