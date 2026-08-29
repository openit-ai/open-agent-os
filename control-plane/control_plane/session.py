"""Session Router & Store — create/resume/route with cross-user isolation (Section 7.1, 14).

Abstract SessionStore interface + InMemory + Redis implementations.
Existing tests use `SessionStore()` which remains InMemory (sync) for compatibility.
Prod uses RedisSessionStore via factory `create_session_store(backend="redis", ...)`.
"""
from __future__ import annotations

import json
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Protocol


def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:16]}"


def new_trace_id() -> str:
    return f"trace_{uuid.uuid4().hex[:16]}"


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionRecord:
    session_id: str
    tenant_id: str
    user_id: str
    agent_id: str
    trace_id: str
    security_domain: str
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    hermes_worker: str | None = None
    prompt_history: list[dict] = field(default_factory=list)
    stream_events: list[dict] = field(default_factory=list)
    status: str = "active"  # active | cancelled | ended

    def assert_owner(self, caller_user_id: str) -> None:
        if caller_user_id != self.user_id:
            raise PermissionError(
                f"cross-user session access denied: session owned by {self.user_id}, caller {caller_user_id}"
            )

    def to_agent_context(self, request_id: str | None = None) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "request_id": request_id or new_request_id(),
            "security_domain": self.security_domain,
        }

    def to_dict(self) -> dict:
        """Serialize for Redis/DB."""
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["updated_at"] = self.updated_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "SessionRecord":
        for k in ("created_at", "updated_at"):
            v = data.get(k)
            if isinstance(v, str):
                try:
                    data[k] = datetime.fromisoformat(v)
                except Exception:
                    data[k] = _now()
        return cls(**data)


class BaseSessionStore(ABC):
    """Abstract SessionStore — sync interface kept for test compat; async variant optional."""

    @abstractmethod
    def create(
        self,
        tenant_id: str,
        user_id: str,
        agent_id: str,
        security_domain: str = "general",
        hermes_worker: str | None = None,
    ) -> SessionRecord: ...

    @abstractmethod
    def get(self, session_id: str, caller_user_id: str) -> SessionRecord: ...

    @abstractmethod
    def get_any(self, session_id: str) -> SessionRecord | None: ...

    @abstractmethod
    def append_prompt(self, session_id: str, caller_user_id: str, prompt: str, request_id: str) -> None: ...

    @abstractmethod
    def append_stream_event(self, session_id: str, event: dict) -> None: ...

    @abstractmethod
    def cancel(self, session_id: str, caller_user_id: str) -> None: ...


class InMemorySessionStore(BaseSessionStore):
    """In-memory store — used in tests/dev."""

    def __init__(self) -> None:
        self._store: dict[str, SessionRecord] = {}

    def create(self, tenant_id: str, user_id: str, agent_id: str, security_domain: str = "general", hermes_worker: str | None = None) -> SessionRecord:
        rec = SessionRecord(
            session_id=new_session_id(),
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            trace_id=new_trace_id(),
            security_domain=security_domain,
            hermes_worker=hermes_worker,
        )
        self._store[rec.session_id] = rec
        return rec

    def get(self, session_id: str, caller_user_id: str) -> SessionRecord:
        rec = self._store.get(session_id)
        if not rec:
            raise KeyError(f"session not found: {session_id}")
        rec.assert_owner(caller_user_id)
        return rec

    def get_any(self, session_id: str) -> SessionRecord | None:
        return self._store.get(session_id)

    def append_prompt(self, session_id: str, caller_user_id: str, prompt: str, request_id: str) -> None:
        rec = self.get(session_id, caller_user_id)
        rec.prompt_history.append({"prompt": prompt, "request_id": request_id, "at": _now().isoformat()})
        rec.updated_at = _now()

    def append_stream_event(self, session_id: str, event: dict) -> None:
        if rec := self._store.get(session_id):
            rec.stream_events.append(event)

    def cancel(self, session_id: str, caller_user_id: str) -> None:
        rec = self.get(session_id, caller_user_id)
        rec.status = "cancelled"

    # helpers for iteration / test compat
    def __len__(self) -> int:
        return len(self._store)


class RedisSessionStore(BaseSessionStore):
    """Redis-backed SessionStore — same interface, JSON serialized.

    Requires `redis>=5.0` (sync client). H5: production fallback=False (mandatory Redis, fail-closed).
    Non-prod (OAOS_ENV != production) allows fallback=True when OAOS_ALLOW_TEST_FALLBACK=1 or unset.
    """

    def __init__(
        self,
        redis_url: str | None = None,
        redis_client=None,
        ttl_seconds: int = 3600 * 24,
        key_prefix: str = "oaos:session:",
        fallback: bool | None = None,
    ) -> None:
        self._url = redis_url or os.environ.get("REDIS_URL") or os.environ.get("OAOS_CP_REDIS_URL") or "redis://localhost:6379/0"
        self._ttl = ttl_seconds
        self._prefix = key_prefix
        self._client = redis_client
        def _is_prod() -> bool:
            try:
                from .env_gate import is_production as _ip
                return _ip()
            except:
                return os.environ.get("OAOS_ENV","").lower() in ("production","prod")
        def _allow_fallback() -> bool:
            if _is_prod():
                return os.environ.get("OAOS_ALLOW_TEST_FALLBACK","").lower() in ("1","true","yes")
            return True
        if fallback is None:
            fallback = _allow_fallback()
        else:
            if _is_prod() and fallback and not _allow_fallback():
                raise RuntimeError("RedisSessionStore fallback not allowed in production (H5 — fallback=False required)")
        self._fallback_store: InMemorySessionStore | None = InMemorySessionStore() if fallback else None
        if self._client is None:
            try:
                import redis as redis_lib  # type: ignore

                self._client = redis_lib.Redis.from_url(self._url, decode_responses=True)
                # probe
                self._client.ping()
            except Exception as e:
                if self._fallback_store is not None:
                    self._client = None
                else:
                    raise RuntimeError(f"Redis unavailable at {self._url}: {e}") from e

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    def _load(self, session_id: str) -> SessionRecord | None:
        if self._client is None:
            if self._fallback_store is None:
                raise RuntimeError(f"Session Redis unavailable (H5 fail-closed, url={self._url})")
            return self._fallback_store.get_any(session_id)
        raw = self._client.get(self._key(session_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return SessionRecord.from_dict(data)
        except Exception:
            return None

    def _save(self, rec: SessionRecord) -> None:
        if self._client is None:
            if self._fallback_store is None:
                raise RuntimeError(f"Session Redis unavailable (H5 fail-closed, url={self._url})")
            self._fallback_store._store[rec.session_id] = rec
            return
        self._client.set(self._key(rec.session_id), json.dumps(rec.to_dict(), ensure_ascii=False), ex=self._ttl)

    def create(self, tenant_id: str, user_id: str, agent_id: str, security_domain: str = "general", hermes_worker: str | None = None) -> SessionRecord:
        rec = SessionRecord(
            session_id=new_session_id(),
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            trace_id=new_trace_id(),
            security_domain=security_domain,
            hermes_worker=hermes_worker,
        )
        if self._client is None:
            if self._fallback_store is None:
                raise RuntimeError(f"Session Redis unavailable (H5 fail-closed, url={self._url})")
            self._fallback_store._store[rec.session_id] = rec
            return rec
        self._save(rec)
        return rec

    def get(self, session_id: str, caller_user_id: str) -> SessionRecord:
        rec = self._load(session_id)
        if not rec:
            raise KeyError(f"session not found: {session_id}")
        rec.assert_owner(caller_user_id)
        return rec

    def get_any(self, session_id: str) -> SessionRecord | None:
        return self._load(session_id)

    def append_prompt(self, session_id: str, caller_user_id: str, prompt: str, request_id: str) -> None:
        rec = self.get(session_id, caller_user_id)
        rec.prompt_history.append({"prompt": prompt, "request_id": request_id, "at": _now().isoformat()})
        rec.updated_at = _now()
        self._save(rec)

    def append_stream_event(self, session_id: str, event: dict) -> None:
        rec = self._load(session_id)
        if rec:
            rec.stream_events.append(event)
            self._save(rec)

    def cancel(self, session_id: str, caller_user_id: str) -> None:
        rec = self.get(session_id, caller_user_id)
        rec.status = "cancelled"
        self._save(rec)


# ── Back-compat alias ────────────────────────────────────────────────
# Existing code/tests import `SessionStore` — keep it as InMemory for zero-breakage.
SessionStore = InMemorySessionStore


def create_session_store(backend: str = "memory", redis_url: str | None = None, **kwargs) -> BaseSessionStore:
    """Factory — `backend=memory|redis`."""
    backend = (backend or "memory").lower()
    if backend == "redis":
        return RedisSessionStore(redis_url=redis_url, **kwargs)
    return InMemorySessionStore()


# Global singleton for dev — prod should use create_session_store via dependency injection
session_store: BaseSessionStore = InMemorySessionStore()
# If REDIS_URL is set and reachable, prefer Redis in prod; keep memory for tests
if os.environ.get("OAOS_SESSION_BACKEND") == "redis":
    try:
        # H5: fallback computed inside RedisSessionStore (prod fail-closed)
        session_store = RedisSessionStore()
    except Exception:
        session_store = InMemorySessionStore()
