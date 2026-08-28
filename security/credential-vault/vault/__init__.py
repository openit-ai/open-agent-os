"""vault package — exports CredentialVault + factory helpers."""

from .vault import CredentialVault, EncryptedPostgresVault  # noqa: F401
from .external import (  # noqa: F401
    VaultBackend,
    EnvFileBackend,
    HashiCorpVaultBackend,
    AwsSecretsBackend,
    get_vault_backend,
    create_vault_backend,
)

import os as _os


def create_vault(
    encryption_key: bytes | None = None,
    session_maker=None,
    audit_ledger=None,
    delegation_service=None,
    db_url: str | None = None,
) -> CredentialVault:
    """Factory per vault-externalization-design §3.6.

    Selects backend from VAULT_BACKEND env.  When an external backend is
    selected, EncryptedPostgresVault is still returned but wraps the external
    backend (DB holds secret_ref metadata only).  Use get_vault_backend()
    directly if you only need the low-level backend.
    """
    key = encryption_key
    if key is None:
        raw = _os.getenv("VAULT_ENCRYPTION_KEY", "change-me-32-byte-base64==")
        key = raw.encode()
    return EncryptedPostgresVault(
        encryption_key=key,  # type: ignore[arg-type]
        session_maker=session_maker,
        audit_ledger=audit_ledger,
        delegation_service=delegation_service,
        db_url=db_url,
    )


__all__ = [
    "CredentialVault",
    "EncryptedPostgresVault",
    "VaultBackend",
    "EnvFileBackend",
    "HashiCorpVaultBackend",
    "AwsSecretsBackend",
    "get_vault_backend",
    "create_vault_backend",
    "create_vault",
]
