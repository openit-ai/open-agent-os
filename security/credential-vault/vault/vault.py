"""Personal Credential Vault — Section 10. Encrypted, never plaintext, never in Hermes process.
- AES-GCM encrypt stub with cryptography.fernet (AES-128-CBC + HMAC-SHA256, AES-GCM 동등 보안 수준)
- owner check (agent_id must match delegation)
- PERSONAL_CREDENTIAL_USE audit event 기록
- DB persistence: when `session_maker` (async_sessionmaker) is provided, vault_credentials table is used;
  otherwise in-memory dict (backward-compatible for tests/dev).
- External backend (Phase B): when VAULT_BACKEND env selects external (hashicorp/aws/env),
  secret bytes are delegated to VaultBackend; DB stores secret_ref + metadata only (dual-write
  controlled by VAULT_DUAL_WRITE / VAULT_READ_FALLBACK). Falls back to Fernet legacy when
  unconfigured so tests require no real Vault/AWS.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import uuid
import warnings
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def _derive_fernet_key(raw_key: bytes) -> bytes:
    """raw bytes → Fernet 32-byte urlsafe key (sha256 → 32bytes → b64)."""
    digest = hashlib.sha256(raw_key).digest()  # 32 bytes
    return base64.urlsafe_b64encode(digest)


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


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

    async def health_check(self) -> bool:
        return True

    def backend_name(self) -> str:
        return "unknown"


class EncryptedPostgresVault(CredentialVault):
    """Fernet(AES-128-CBC + HMAC) encrypted column (Section 10.2) + external backend.

    Dual-mode:
      - In-memory dict (default): `session_maker is None` → existing behavior, tests pass without DB.
      - DB-backed: provide `session_maker` (async_sessionmaker) or `db_url` → vault_credentials table.
    External mode: when VAULT_BACKEND selects an external backend (hashicorp/aws/env/auto),
    secret bytes are stored in VaultBackend; DB stores secret_ref+metadata only. Dual-write
    and read-fallback are controlled by VAULT_DUAL_WRITE / VAULT_READ_FALLBACK env flags
    during migration. When no external backend is configured, legacy Fernet path is used
    (with DeprecationWarning as signal for operators).

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

        # ── external backend wiring (Phase B) ──
        self._external = None  # type: ignore
        try:
            from .external import get_vault_backend  # lazy to avoid circular
            self._external = get_vault_backend()
        except ValueError:
            raise
        except Exception as e:
            logger.debug("vault external backend init skipped: %s", e)
            self._external = None

        if self._external is None:
            # legacy path — emit deprecation only when not explicitly set to legacy
            # (avoid spamming tests; only warn when VAULT_BACKEND is legacy/empty)
            warnings.warn(
                "EncryptedPostgresVault using legacy encrypted_postgres backend (encrypted_token in DB). "
                "Set VAULT_BACKEND=hashicorp_vault or aws_secrets for externalized secrets.",
                DeprecationWarning,
                stacklevel=2,
            )
            logger.info("Vault: using legacy Fernet backend (no external VAULT_BACKEND configured)")
        else:
            logger.info("Vault: external backend active (%s)", self._external.backend_name())

    def set_session_maker(self, session_maker) -> None:
        """Inject/replace DB session maker at runtime (e.g. from FastAPI lifespan)."""
        self._session_maker = session_maker

    @property
    def _use_external(self) -> bool:
        return self._external is not None

    def _dual_write(self) -> bool:
        return _env_flag("VAULT_DUAL_WRITE")

    def _read_fallback(self) -> bool:
        return _env_flag("VAULT_READ_FALLBACK")

    def backend_name(self) -> str:  # type: ignore[override]
        if self._external is not None:
            try:
                return self._external.backend_name()
            except Exception:
                return "external"
        return "encrypted_postgres"

    async def health_check(self) -> bool:  # type: ignore[override]
        if self._external is not None:
            try:
                return await self._external.health_check()
            except Exception:
                return False
        return True

    # ── internal DB helpers (lazy import to avoid hard dep in tests) ─────
    async def _db_insert(self, secret_ref: str, user_id: str, owner_agent_id: str, provider: str, scope: str, encrypted: bytes | None) -> None:
        from security.models.orm import VaultCredentialORM  # lazy

        async with self._session_maker() as sess:  # type: ignore
            # build row tolerant of new columns existing or not
            row_kwargs = dict(
                secret_ref=secret_ref,
                user_id=user_id,
                owner_agent_id=owner_agent_id,
                provider=provider,
                scope=scope,
                encrypted_token=encrypted,
                created_at=datetime.now(timezone.utc),
            )
            # optional externalization cols — set if model has them
            try:
                # check if column exists on mapper
                mapper_cols = {c.key for c in VaultCredentialORM.__table__.columns}
                if "vault_backend" in mapper_cols:
                    row_kwargs["vault_backend"] = self.backend_name()
                if "vault_path" in mapper_cols:
                    # follow design doc path mapping
                    prefix = os.getenv("VAULT_KV_PREFIX", "openagentos/")
                    mount = os.getenv("VAULT_KV_MOUNT", "secret")
                    if self.backend_name() == "hashicorp_vault":
                        row_kwargs["vault_path"] = f"{mount}/data/{prefix}{secret_ref}"
                    elif self.backend_name() == "aws_secrets":
                        aws_prefix = os.getenv("AWS_SECRETS_PREFIX", "openagentos/")
                        row_kwargs["vault_path"] = f"{aws_prefix}{secret_ref}"
                    else:
                        row_kwargs["vault_path"] = None
                if "version" in mapper_cols:
                    row_kwargs["version"] = 1  # type: ignore[assignment]
            except Exception:
                pass
            row = VaultCredentialORM(**row_kwargs)  # type: ignore[arg-type]
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

        # ── external path ─────────────────────────────────────
        if self._use_external:
            # 1) write to external backend first (fail closed: no DB row if this fails)
            metadata = {"user_id": user_id, "provider": provider, "scope": scope}
            await self._external.put(ref, token, metadata)  # type: ignore

            # 2) determine what to store in DB's encrypted_token column
            encrypted: bytes | None = None
            if self._dual_write():
                # dual-write: also store Fernet ciphertext for rollback window
                try:
                    encrypted = self._fernet.encrypt(token)
                except Exception:
                    encrypted = None
                    logger.warning("dual-write Fernet encrypt failed for %s", ref)

            # 3) write DB metadata row (or fallback to memory)
            if self._session_maker is not None:
                try:
                    await self._db_insert(ref, user_id, owner_agent_id, provider, scope, encrypted)
                    self._meta[ref] = {
                        "user_id": user_id,
                        "owner_agent_id": owner_agent_id,
                        "provider": provider,
                        "scope": scope,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    return ref
                except Exception as e:
                    # DB failed after external write — cleanup external secret to avoid orphan
                    logger.warning("vault DB insert failed after external put %s: %s", ref, e)
                    try:
                        await self._external.delete(ref)  # type: ignore
                    except Exception as ce:
                        logger.debug("external cleanup failed for %s: %s", ref, ce)
                    raise

            # DB not available: keep in memory meta + external already holds bytes
            # For in-memory fallback we keep encrypted only if dual_write else None
            if self._dual_write() and encrypted is not None:
                self._store[ref] = encrypted
            else:
                # store placeholder so owner_of works; actual bytes are in external
                self._store.pop(ref, None)
            self._meta[ref] = {
                "user_id": user_id,
                "owner_agent_id": owner_agent_id,
                "provider": provider,
                "scope": scope,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            return ref

        # ── legacy Fernet path ─────────────────────────────────
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
        # fetch meta for owner check (DB preferred)
        meta: dict | None = None
        encrypted: bytes | None = None
        row = None

        if self._session_maker is not None:
            try:
                row = await self._db_get(secret_ref)
                if row is not None:
                    meta = {
                        "user_id": row.user_id,
                        "owner_agent_id": row.owner_agent_id,
                        "provider": row.provider,
                        "scope": row.scope,
                    }
                    # row may have encrypted_token None when externalized
                    encrypted = getattr(row, "encrypted_token", None)
                else:
                    meta = self._meta.get(secret_ref)
                    encrypted = self._store.get(secret_ref)
            except Exception:
                meta = self._meta.get(secret_ref)
                encrypted = self._store.get(secret_ref)
        else:
            meta = self._meta.get(secret_ref)
            encrypted = self._store.get(secret_ref)

        # if neither DB nor memory has meta, still need to know owner — check memory as last resort
        if meta is None:
            meta = self._meta.get(secret_ref)
            if encrypted is None:
                encrypted = self._store.get(secret_ref)

        # owner check BEFORE touching external backend (design §5) — fail closed if owner unknown, but distinguish not-found
        owner = (meta or {}).get("owner_agent_id")
        # For non-external, existence is known now: if no meta and no encrypted, secret not found -> KeyError
        if not self._use_external:
            if meta is None and encrypted is None:
                raise KeyError(f"secret not found: {secret_ref}")
            if not owner or requester_agent_id != owner:
                raise PermissionError(f"credential isolation violation: owner={owner} requester={requester_agent_id}")
        else:
            # external: if we have meta, enforce owner immediately (fail-closed even before external fetch)
            if meta is not None:
                if not owner or requester_agent_id != owner:
                    raise PermissionError(f"credential isolation violation: owner={owner} requester={requester_agent_id}")
            # if meta is None, defer owner check until after external probe (orphan external secret still fail-closed)

        # ── external path ─────────────────────────────────────
        if self._use_external:
            # if we have no meta at all (orphan in external only), allow external fallback but still need owner check via DB row
            # owner check already done if meta present; if meta missing we already attempted DB fetch so remain.
            try:
                ext_bytes = await self._external.get(secret_ref)  # type: ignore
            except Exception as e:
                logger.warning("external vault get failed for %s: %s", secret_ref, e)
                ext_bytes = None

            if ext_bytes is not None:
                # orphan external secret with no owner -> fail closed (do not leak existence)
                if meta is None or not (meta or {}).get("owner_agent_id"):
                    # try to re-fetch owner from DB one more time if possible, otherwise deny
                    if meta is None:
                        # attempt DB owner lookup if available
                        try:
                            db_owner = await self._owner_from_db(secret_ref)
                            if db_owner:
                                if requester_agent_id != db_owner:
                                    raise PermissionError(f"credential isolation violation: owner={db_owner} requester={requester_agent_id}")
                                meta = {"provider": None, "user_id": None, "owner_agent_id": db_owner}
                            else:
                                raise PermissionError(f"credential isolation violation: owner=None requester={requester_agent_id}")
                        except PermissionError:
                            raise
                        except Exception:
                            raise PermissionError(f"credential isolation violation: owner=None requester={requester_agent_id}")
                    else:
                        raise PermissionError(f"credential isolation violation: owner={owner} requester={requester_agent_id}")
                # decrypt is not needed — external already holds plaintext
                return await self._audit_and_return(secret_ref, requester_agent_id, meta, ext_bytes)

            # external miss — try fallback if enabled
            if self._read_fallback() and encrypted is not None:
                # legacy ciphertext fallback
                try:
                    plaintext = self._fernet.decrypt(encrypted)
                    if meta is None:
                        meta = {"provider": None, "user_id": None}
                    return await self._audit_and_return(secret_ref, requester_agent_id, meta, plaintext)
                except InvalidToken as e:
                    raise ValueError("decryption failed — invalid key or corrupted token") from e
            # not found in both
            raise KeyError(f"secret not found: {secret_ref}")

        # ── legacy path ───────────────────────────────────────
        # need encrypted for legacy
        if encrypted is None:
            # try to reload from row if we missed
            if row is not None:
                encrypted = getattr(row, "encrypted_token", None)
            if encrypted is None:
                encrypted = self._store.get(secret_ref)
        if encrypted is None:
            raise KeyError(f"secret not found: {secret_ref}")

        if not owner or requester_agent_id != owner:
            raise PermissionError(f"credential isolation violation: owner={owner} requester={requester_agent_id}")

        try:
            plaintext = self._fernet.decrypt(encrypted)
        except InvalidToken as e:
            raise ValueError("decryption failed — invalid key or corrupted token") from e

        return await self._audit_and_return(secret_ref, requester_agent_id, meta or {}, plaintext)

    async def _audit_and_return(self, secret_ref: str, requester_agent_id: str, meta: dict, plaintext: bytes) -> bytes:
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
        if self._use_external:
            try:
                await self._external.delete(secret_ref)  # type: ignore
            except Exception as e:
                logger.debug("external vault delete failed for %s: %s", secret_ref, e)
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
        # check in-memory first
        meta = self._meta.get(secret_ref)
        if meta and meta.get("owner_agent_id"):
            return meta.get("owner_agent_id")
        # fall back to DB lookup if available (sync attempt via asyncio.run when possible)
        if self._session_maker is not None:
            try:
                import asyncio
                # if running loop, cannot block — try to avoid deadlock
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is None or not loop.is_running():
                    # safe to run async helper
                    owner = asyncio.run(self._owner_from_db(secret_ref))
                    if owner:
                        return owner
                else:
                    # running loop — try to check via sync query if possible (best-effort)
                    # attempt to create a new loop in thread
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                        fut = ex.submit(asyncio.run, self._owner_from_db(secret_ref))
                        try:
                            owner = fut.result(timeout=2)
                            if owner:
                                return owner
                        except Exception:
                            pass
            except Exception:
                pass
        return None

    # Back-compat: allow tests to query ownership even when DB holds truth
    async def _owner_from_db(self, secret_ref: str) -> str | None:
        if self._session_maker is None:
            return self.owner_of(secret_ref)
        try:
            row = await self._db_get(secret_ref)
            if row is not None:
                return row.owner_agent_id
        except Exception:
            pass
        return self.owner_of(secret_ref)
