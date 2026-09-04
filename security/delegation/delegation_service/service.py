"""Delegation Service — Section 9. User → Agent consent.
- grant/revoke with status
- in-memory + secure hash (sha256 delegation fingerprint)
- revoke 시 credential binding 무효 (cascade) + MemoryStore + Vault cascade
- DB persistence: when DATABASE_URL/OAOS_DATABASE_URL is set, delegations &
  credential_bindings are persisted to delegations / credential_bindings tables
  (sync SQLAlchemy, sqlite compat). Falls back to in-memory dicts when no DB
  or on DB error. All DB imports are lazy.
"""
from __future__ import annotations

import hashlib
import uuid
import os
import logging
import time
import threading
from datetime import datetime, timezone

from delegation_model import CredentialBinding, CredentialBindingStatus, Delegation, DelegationStatus

logger = logging.getLogger(__name__)

# ── Vault revoke retry metrics (shared with vault module) ─────────────
# Prometheus counter oaos_vault_revoke_failures_total + dead-letter log
_delegation_vault_revoke_failures_total: int = 0
_delegation_vault_revoke_failures_lock = threading.Lock()
_delegation_vault_dead_letters: list[dict] = []
_delegation_vault_dead_letters_lock = threading.Lock()
_VAULT_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)


def _inc_delegation_vault_revoke_failures() -> None:
    global _delegation_vault_revoke_failures_total
    with _delegation_vault_revoke_failures_lock:
        _delegation_vault_revoke_failures_total += 1


def get_delegation_vault_revoke_failures_total() -> int:
    with _delegation_vault_revoke_failures_lock:
        return _delegation_vault_revoke_failures_total


def get_delegation_vault_dead_letters() -> list[dict]:
    with _delegation_vault_dead_letters_lock:
        return list(_delegation_vault_dead_letters)


def delegation_vault_revoke_metrics_prometheus() -> str:
    # unified metric name — include delegation-side failures
    # vault module already exposes same name; this aggregates for convenience
    total = get_delegation_vault_revoke_failures_total()
    # also try to add vault module total for total view
    try:
        from vault.vault import get_vault_revoke_failures_total  # type: ignore

        total += get_vault_revoke_failures_total()
    except Exception:
        try:
            from security.credential_vault.vault.vault import get_vault_revoke_failures_total as _g  # type: ignore

            total += _g()
        except Exception:
            pass
    lines = [
        "# HELP oaos_vault_revoke_failures_total Vault revoke failures after retries (dead-letter)",
        "# TYPE oaos_vault_revoke_failures_total counter",
        f"oaos_vault_revoke_failures_total {total}",
    ]
    return "\n".join(lines) + "\n"


def _should_skip_sleep() -> bool:
    return os.getenv("OAOS_VAULT_REVOKE_SLEEP", "").strip() == "0"


def _record_delegation_dead_letter(secret_ref: str, delegation_id: str, error: str) -> None:
    entry = {
        "secret_ref": secret_ref,
        "delegation_id": delegation_id,
        "error": str(error)[:500],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with _delegation_vault_dead_letters_lock:
        _delegation_vault_dead_letters.append(entry)
    logger.error(
        "vault revoke dead-letter delegation=%s secret_ref=%s error=%s",
        delegation_id,
        secret_ref,
        error,
        extra={"secret_ref": secret_ref, "delegation_id": delegation_id, "dead_letter": True},
    )
    _inc_delegation_vault_revoke_failures()
    try:
        from execution_gateway.metrics import default_metrics  # type: ignore
        default_metrics.record_vault_revoke_failure()
    except Exception:
        try:
            from execution_gateway.execution_gateway.metrics import default_metrics as _dm2  # type: ignore
            _dm2.record_vault_revoke_failure()
        except Exception:
            pass


def _delegation_hash(d: Delegation) -> str:
    """위임 fingerprint — 변조 탐지용 secure hash."""
    raw = f"{d.id}|{d.user_id}|{d.agent_id}|{d.provider}|{d.scope}|{d.status.value}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ── DB helpers (lazy, sync) ──────────────────────────────────────────

def _is_production() -> bool:
    return any(
        os.environ.get(key, "").strip().lower() in ("production", "prod")
        for key in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT")
    )


def _db_enabled() -> bool:
    url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    return bool(url and url.strip())


def _normalize_sync_url(url: str) -> str:
    u = url.strip()
    if "+asyncpg" in u:
        u = u.replace("+asyncpg", "")
    if "+aiosqlite" in u:
        u = u.replace("+aiosqlite", "")
    if u.startswith("postgresql+asyncpg://"):
        u = u.replace("postgresql+asyncpg://", "postgresql://", 1)
    if u.startswith("sqlite+aiosqlite://"):
        u = u.replace("sqlite+aiosqlite://", "sqlite://", 1)
    return u


def _db_sync_url() -> str | None:
    url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url or not url.strip():
        return None
    return _normalize_sync_url(url.strip())


def _db_get_session():
    url = _db_sync_url()
    if not url:
        return None, None
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
    except Exception:
        return None, None
    try:
        connect_args = {}
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        engine = create_engine(url, echo=False, pool_pre_ping=False, connect_args=connect_args)
        # ensure tables exist (idempotent)
        try:
            from security.models.db import Base  # type: ignore
            from security.models.orm import DelegationORM, CredentialBindingORM  # noqa: F401  # type: ignore
            Base.metadata.create_all(bind=engine)
        except Exception:
            # fallback: try via file path
            try:
                import sys
                from pathlib import Path
                sec = Path(__file__).resolve().parents[2]
                if str(sec) not in sys.path:
                    sys.path.insert(0, str(sec))
                from security.models.db import Base  # type: ignore
                from security.models.orm import DelegationORM, CredentialBindingORM  # noqa: F401  # type: ignore
                Base.metadata.create_all(bind=engine)
            except Exception:
                pass
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()
        return session, engine
    except Exception as e:
        logger.debug("DelegationService DB session failed: %s", e)
        return None, None


def _db_close(session, engine) -> None:
    try:
        if session is not None:
            session.close()
    except Exception:
        pass
    try:
        if engine is not None:
            engine.dispose()
    except Exception:
        pass


def _delegation_to_orm(d: Delegation):
    try:
        from security.models.orm import DelegationORM  # type: ignore
    except ImportError:
        import sys
        from pathlib import Path
        sec = Path(__file__).resolve().parents[2]
        if str(sec) not in sys.path:
            sys.path.insert(0, str(sec))
        from security.models.orm import DelegationORM  # type: ignore
    return DelegationORM(
        id=d.id,
        user_id=d.user_id,
        agent_id=d.agent_id,
        provider=d.provider,
        scope=d.scope,
        status=d.status.value if hasattr(d.status, "value") else str(d.status),
        created_at=d.created_at,
        expires_at=d.expires_at,
        revoked_at=d.revoked_at,
    )


def _delegation_from_orm(row) -> Delegation:
    status_val = getattr(row, "status", "ACTIVE")
    try:
        status = DelegationStatus(status_val)
    except Exception:
        status = DelegationStatus.ACTIVE if status_val == "ACTIVE" else DelegationStatus.REVOKED
    created_at = getattr(row, "created_at")
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    expires_at = getattr(row, "expires_at")
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    revoked_at = getattr(row, "revoked_at")
    if revoked_at is not None and revoked_at.tzinfo is None:
        revoked_at = revoked_at.replace(tzinfo=timezone.utc)
    return Delegation(
        id=str(row.id),
        user_id=str(row.user_id),
        agent_id=str(row.agent_id),
        provider=str(row.provider),
        scope=str(row.scope),
        status=status,
        created_at=created_at,
        expires_at=expires_at,
        revoked_at=revoked_at,
    )


def _binding_to_orm(b: CredentialBinding):
    try:
        from security.models.orm import CredentialBindingORM  # type: ignore
    except ImportError:
        import sys
        from pathlib import Path
        sec = Path(__file__).resolve().parents[2]
        if str(sec) not in sys.path:
            sys.path.insert(0, str(sec))
        from security.models.orm import CredentialBindingORM  # type: ignore
    return CredentialBindingORM(
        id=b.id,
        delegation_id=b.delegation_id,
        provider=b.provider,
        secret_ref=b.secret_ref,
        scope=b.scope,
        status=b.status.value if hasattr(b.status, "value") else str(b.status),
        expires_at=b.expires_at,
        last_used_at=getattr(b, "last_used_at", None),
    )


def _binding_from_orm(row) -> CredentialBinding:
    status_val = getattr(row, "status", "ACTIVE")
    try:
        status = CredentialBindingStatus(status_val)
    except Exception:
        status = CredentialBindingStatus.ACTIVE if status_val == "ACTIVE" else CredentialBindingStatus.REVOKED
    expires_at = getattr(row, "expires_at")
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    last_used_at = getattr(row, "last_used_at", None)
    if last_used_at is not None and last_used_at.tzinfo is None:
        last_used_at = last_used_at.replace(tzinfo=timezone.utc)
    return CredentialBinding(
        id=str(row.id),
        delegation_id=str(row.delegation_id),
        provider=str(row.provider),
        secret_ref=str(row.secret_ref),
        scope=str(row.scope),
        status=status,
        expires_at=expires_at,
        last_used_at=last_used_at,
    )


class DelegationService:
    """In-memory delegation & credential binding store with optional DB persistence.

    - grant: Delegation 생성 + secure hash 기록
    - bind_credential: CredentialBinding 생성 (secret_ref 연결)
    - revoke: Delegation REVOKED + 연결된 모든 CredentialBinding REVOKED + MemoryStore + Vault cascade
    When DATABASE_URL is set, all operations are persisted to delegations /
    credential_bindings tables (sync SQLAlchemy, sqlite compat). Falls back to
    in-memory dicts on any DB error.
    """

    def __init__(self, vault=None, memory_store=None) -> None:
        self._store: dict[str, Delegation] = {}
        self._hash_store: dict[str, str] = {}  # delegation_id → sha256 hash
        self._bindings: dict[str, CredentialBinding] = {}  # binding_id → binding
        self._delegation_bindings: dict[str, set[str]] = {}  # delegation_id → set(binding_id)
        self._vault = vault
        self._memory_store = memory_store

    def set_vault(self, vault) -> None:
        self._vault = vault

    def set_memory_store(self, store) -> None:
        self._memory_store = store

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
        # DB persist (best-effort)
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is None:
                    if _is_production():
                        raise RuntimeError("delegation database unavailable in production")
                else:
                    try:
                        orm = _delegation_to_orm(d)
                        session.add(orm)
                        session.commit()
                    except Exception as e:
                        try:
                            session.rollback()
                        except Exception:
                            pass
                        if _is_production():
                            raise RuntimeError("delegation database persist failed in production") from e
                        logger.debug("Delegation grant DB persist failed: %s", e)
                    finally:
                        _db_close(session, engine)
            except Exception:
                if _is_production():
                    self._store.pop(d.id, None)
                    self._hash_store.pop(d.id, None)
                    self._delegation_bindings.pop(d.id, None)
                    raise
        return d

    def revoke(self, delegation_id: str) -> Delegation | None:
        """Revoke delegation + cascade to credential bindings + MemoryStore + Vault."""
        # try DB first to get current state
        d = self._store.get(delegation_id)
        # if not in memory but DB enabled, load from DB
        if d is None and _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        from security.models.orm import DelegationORM  # type: ignore

                        row = session.query(DelegationORM).filter(DelegationORM.id == delegation_id).first()  # type: ignore
                        if row is not None:
                            d = _delegation_from_orm(row)
                            self._store[d.id] = d
                            self._hash_store[d.id] = _delegation_hash(d)
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass
        if d is None:
            return None
        if d.status == DelegationStatus.REVOKED:
            return d
        d.status = DelegationStatus.REVOKED
        d.revoked_at = datetime.now(timezone.utc)
        self._hash_store[d.id] = _delegation_hash(d)
        self._store[d.id] = d
        # DB update
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        from security.models.orm import DelegationORM, CredentialBindingORM  # type: ignore

                        row = session.query(DelegationORM).filter(DelegationORM.id == delegation_id).first()  # type: ignore
                        if row is not None:
                            row.status = "REVOKED"
                            row.revoked_at = d.revoked_at
                        # cascade bindings in DB
                        try:
                            bindings = session.query(CredentialBindingORM).filter(CredentialBindingORM.delegation_id == delegation_id).all()  # type: ignore
                            for br in bindings:
                                if getattr(br, "status", "ACTIVE") == "ACTIVE":
                                    br.status = "REVOKED"
                        except Exception:
                            pass
                        session.commit()
                    except Exception as e:
                        try:
                            session.rollback()
                        except Exception:
                            pass
                        logger.debug("Delegation revoke DB update failed: %s", e)
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass
        # cascade: 모든 연결된 binding 무효화 (in-memory)
        for bid in self._delegation_bindings.get(delegation_id, set()).copy():
            b = self._bindings.get(bid)
            if b and b.status == CredentialBindingStatus.ACTIVE:
                b.status = CredentialBindingStatus.REVOKED
        # also load bindings from DB if not in memory (best-effort)
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        from security.models.orm import CredentialBindingORM  # type: ignore

                        rows = session.query(CredentialBindingORM).filter(CredentialBindingORM.delegation_id == delegation_id).all()  # type: ignore
                        for r in rows:
                            bid = str(r.id)
                            # update in-memory if present
                            if bid in self._bindings:
                                if self._bindings[bid].status == CredentialBindingStatus.ACTIVE:
                                    self._bindings[bid].status = CredentialBindingStatus.REVOKED
                            else:
                                # hydrate
                                try:
                                    b2 = _binding_from_orm(r)
                                    b2.status = CredentialBindingStatus.REVOKED
                                    self._bindings[bid] = b2
                                except Exception:
                                    pass
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass
        # ── revoke cascade: MemoryStore + Vault (lazy, best-effort) ──
        # MemoryStore.invalidate_by_delegation
        try:
            store = self._memory_store
            if store is None:
                try:
                    from governance.governance import get_default_store  # type: ignore

                    store = get_default_store()
                except ImportError:
                    try:
                        from security.memory_governance.governance.governance import get_default_store as _gds  # type: ignore

                        store = _gds()
                    except Exception:
                        store = None
                except Exception:
                    store = None
            if store is not None:
                try:
                    store.invalidate_by_delegation(delegation_id, reason="delegation_revoked")
                except Exception as e:
                    logger.debug("MemoryStore invalidate_by_delegation failed: %s", e)
        except Exception:
            pass
        # Vault revoke: revoke each secret_ref for this delegation
        try:
            vault = self._vault
            # try to discover vault from app singleton if not injected
            if vault is None:
                try:
                    import sys

                    # check if security app has vault singleton
                    if "security_app_module" in sys.modules:
                        mod = sys.modules["security_app_module"]
                        vault = getattr(mod, "vault_instance", None) or getattr(mod, "vault", None)
                except Exception:
                    vault = None
            if vault is not None:
                # collect secret_refs for this delegation
                secret_refs: list[str] = []
                for bid in self._delegation_bindings.get(delegation_id, set()):
                    b = self._bindings.get(bid)
                    if b:
                        secret_refs.append(b.secret_ref)
                # also query DB for secret_refs
                if _db_enabled():
                    try:
                        session2, engine2 = _db_get_session()
                        if session2 is not None:
                            try:
                                from security.models.orm import CredentialBindingORM  # type: ignore

                                rows = session2.query(CredentialBindingORM).filter(CredentialBindingORM.delegation_id == delegation_id).all()  # type: ignore
                                for r in rows:
                                    sr = str(getattr(r, "secret_ref", ""))
                                    if sr and sr not in secret_refs:
                                        secret_refs.append(sr)
                            finally:
                                _db_close(session2, engine2)
                    except Exception:
                        pass
                for sr in secret_refs:
                    import asyncio as _asyncio
                    import inspect as _inspect

                    _is_async = _inspect.iscoroutinefunction(getattr(vault.revoke, "__wrapped__", vault.revoke))
                    if _is_async:
                        try:
                            _loop = _asyncio.get_event_loop()
                            _running = _loop.is_running()
                        except RuntimeError:
                            _loop = None  # type: ignore
                            _running = False
                        if _running and _loop is not None:
                            async def _deleg_async_retry(_sr=sr, _vid=delegation_id):
                                for a in range(3):
                                    try:
                                        await vault.revoke(_sr)
                                        return
                                    except Exception as e2:
                                        if a < 2:
                                            dly = _VAULT_RETRY_DELAYS[a]
                                            logger.warning(
                                                "delegation vault revoke (async task) attempt %d/3 failed sr=%s: %s — retry in %ss",
                                                a + 1, _sr, e2, dly,
                                            )
                                            if not _should_skip_sleep():
                                                await _asyncio.sleep(dly)
                                        else:
                                            logger.warning(
                                                "delegation vault revoke (async task) attempt %d/3 failed sr=%s: %s",
                                                a + 1, _sr, e2,
                                            )
                                            if not _should_skip_sleep():
                                                await _asyncio.sleep(_VAULT_RETRY_DELAYS[2])
                                            _record_delegation_dead_letter(_sr, _vid, str(e2))

                            try:
                                _loop.create_task(_deleg_async_retry())  # type: ignore[attr-defined]
                            except Exception as e:
                                logger.debug("Failed to schedule vault revoke task for %s: %s", sr, e)
                            continue
                        else:
                            last_exc = None
                            for attempt in range(3):
                                try:
                                    _asyncio.run(vault.revoke(sr))  # type: ignore[arg-type]
                                    last_exc = None
                                    break
                                except Exception as e:
                                    last_exc = e
                                    if attempt < 2:
                                        dly = _VAULT_RETRY_DELAYS[attempt]
                                        logger.warning(
                                            "delegation vault revoke attempt %d/3 failed sr=%s: %s — retry in %ss",
                                            attempt + 1, sr, e, dly,
                                        )
                                        if not _should_skip_sleep():
                                            time.sleep(dly)
                                    else:
                                        logger.warning(
                                            "delegation vault revoke attempt %d/3 failed sr=%s: %s — no more retries",
                                            attempt + 1, sr, e,
                                        )
                                        if not _should_skip_sleep():
                                            time.sleep(_VAULT_RETRY_DELAYS[2])
                                        _record_delegation_dead_letter(sr, delegation_id, str(e))
                            if last_exc is not None:
                                logger.debug("Vault revoke failed for %s after retries: %s", sr, last_exc)
                            continue
                    else:
                        last_exc = None  # type: ignore[no-redef]
                        for attempt in range(3):
                            try:
                                vault.revoke(sr)  # type: ignore
                                last_exc = None
                                break
                            except Exception as e:
                                last_exc = e
                                if attempt < 2:
                                    dly = _VAULT_RETRY_DELAYS[attempt]
                                    logger.warning(
                                        "delegation vault revoke attempt %d/3 failed sr=%s: %s — retry in %ss",
                                        attempt + 1, sr, e, dly,
                                    )
                                    if not _should_skip_sleep():
                                        time.sleep(dly)
                                else:
                                    logger.warning(
                                        "delegation vault revoke attempt %d/3 failed sr=%s: %s — no more retries",
                                        attempt + 1, sr, e,
                                    )
                                    if not _should_skip_sleep():
                                        time.sleep(_VAULT_RETRY_DELAYS[2])
                                    _record_delegation_dead_letter(sr, delegation_id, str(e))
                        if last_exc is not None:
                            logger.debug("Vault revoke failed for %s after retries: %s", sr, last_exc)
                        continue
        except Exception:
            pass
        return d

    def is_active(self, delegation_id: str) -> bool:
        d = self.get(delegation_id)
        if d is None:
            return False
        if d.status != DelegationStatus.ACTIVE:
            return False
        if d.expires_at and d.expires_at < datetime.now(timezone.utc):
            return False
        return True

    def get(self, delegation_id: str) -> Delegation | None:
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is None:
                    if _is_production():
                        raise RuntimeError("delegation database unavailable in production")
                else:
                    try:
                        from security.models.orm import DelegationORM  # type: ignore

                        row = session.query(DelegationORM).filter(DelegationORM.id == delegation_id).first()  # type: ignore
                        if row is not None:
                            d = _delegation_from_orm(row)
                            self._store[d.id] = d
                            self._hash_store[d.id] = _delegation_hash(d)
                            return d
                    finally:
                        _db_close(session, engine)
            except Exception:
                if _is_production():
                    raise
            # if DB enabled but row not found in DB, fallback to memory (may be uncommitted)
            return self._store.get(delegation_id)
        return self._store.get(delegation_id)

    def verify_hash(self, delegation_id: str) -> bool:
        """저장된 hash 와 현재 delegation 상태 hash 비교 — 변조 탐지."""
        d = self.get(delegation_id)
        if d is None:
            return False
        # if hash not in memory, compute (DB case)
        stored = self._hash_store.get(d.id)
        if stored is None:
            # try to recompute and store
            self._hash_store[d.id] = _delegation_hash(d)
            return True
        return stored == _delegation_hash(d)

    def list_by_user(self, user_id: str) -> list[Delegation]:
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        from security.models.orm import DelegationORM  # type: ignore

                        rows = session.query(DelegationORM).filter(DelegationORM.user_id == user_id).all()  # type: ignore
                        result = []
                        for r in rows:
                            d = _delegation_from_orm(r)
                            self._store[d.id] = d
                            self._hash_store[d.id] = _delegation_hash(d)
                            result.append(d)
                        return result
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass
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
        d = self.get(delegation_id)
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
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is None:
                    if _is_production():
                        raise RuntimeError("credential binding database unavailable in production")
                else:
                    try:
                        orm = _binding_to_orm(b)
                        session.add(orm)
                        session.commit()
                    except Exception as e:
                        try:
                            session.rollback()
                        except Exception:
                            pass
                        if _is_production():
                            raise RuntimeError("credential binding database persist failed in production") from e
                        logger.debug("bind_credential DB persist failed: %s", e)
                    finally:
                        _db_close(session, engine)
            except Exception:
                if _is_production():
                    self._bindings.pop(b.id, None)
                    self._delegation_bindings.get(delegation_id, set()).discard(b.id)
                    raise
        return b

    def get_binding(self, binding_id: str) -> CredentialBinding | None:
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is None:
                    if _is_production():
                        raise RuntimeError("credential binding database unavailable in production")
                else:
                    try:
                        from security.models.orm import CredentialBindingORM  # type: ignore

                        row = session.query(CredentialBindingORM).filter(CredentialBindingORM.id == binding_id).first()  # type: ignore
                        if row is not None:
                            b = _binding_from_orm(row)
                            self._bindings[b.id] = b
                            return b
                    finally:
                        _db_close(session, engine)
            except Exception:
                if _is_production():
                    raise
        return self._bindings.get(binding_id)

    def list_bindings_for_delegation(self, delegation_id: str) -> list[CredentialBinding]:
        """Return bindings for a delegation, loading the durable rows first.

        Execution Gateway processes are separate from the Control Plane, so
        the process-local ``_delegation_bindings`` index is empty after a
        restart.  GoogleConnector uses this method to resolve an active
        ``secret_ref`` from the shared OAOS database rather than relying on a
        stale in-memory cache.
        """
        if not delegation_id:
            return []
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is None:
                    if _is_production():
                        raise RuntimeError("credential binding database unavailable in production")
                else:
                    try:
                        from security.models.orm import CredentialBindingORM  # type: ignore

                        rows = session.query(CredentialBindingORM).filter(
                            CredentialBindingORM.delegation_id == delegation_id
                        ).all()  # type: ignore
                        result: list[CredentialBinding] = []
                        ids: set[str] = set()
                        for row in rows:
                            binding = _binding_from_orm(row)
                            self._bindings[binding.id] = binding
                            ids.add(binding.id)
                            result.append(binding)
                        self._delegation_bindings[delegation_id] = ids
                        return result
                    finally:
                        _db_close(session, engine)
            except Exception as e:
                if _is_production():
                    raise RuntimeError("credential binding lookup failed in production") from e
                logger.debug("binding list DB lookup failed: %s", e)
        ids = self._delegation_bindings.get(delegation_id, set())
        return [self._bindings[binding_id] for binding_id in ids if binding_id in self._bindings]

    def is_binding_active(self, binding_id: str) -> bool:
        b = self.get_binding(binding_id)
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
        b = self.get_binding(binding_id)
        if b:
            b.status = CredentialBindingStatus.REVOKED
            self._bindings[b.id] = b
            if _db_enabled():
                try:
                    session, engine = _db_get_session()
                    if session is not None:
                        try:
                            from security.models.orm import CredentialBindingORM  # type: ignore

                            row = session.query(CredentialBindingORM).filter(CredentialBindingORM.id == binding_id).first()  # type: ignore
                            if row is not None:
                                row.status = "REVOKED"
                                session.commit()
                        except Exception:
                            try:
                                session.rollback()
                            except Exception:
                                pass
                        finally:
                            _db_close(session, engine)
                except Exception:
                    pass
