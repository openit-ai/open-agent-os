"""Session manager — §16C.1

Tenant+agent isolated, in-memory default with optional Redis / openagentos DB.
No hard dependencies: redis / sqlalchemy are optional and imported lazily.

API:
  SessionManager().create(tenant_id, agent_id, ...) -> SessionRecord dict
  SessionManager().resume(session_id, tenant_id, agent_id) -> dict
  SessionManager().cancel(session_id, tenant_id, agent_id) -> dict
  SessionManager().get_state(session_id, tenant_id, agent_id) -> dict
  OAOSContext — pydantic-ai inspired deps_type, injected into every tool
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str, n: int = 16) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:n]}"


def _vault_path_for(tenant_id: str, agent_id: str) -> str:
    # Deterministic vault namespace per tenant/agent — matches vault externalization
    safe_t = tenant_id.replace("/", "_").strip() or "default"
    safe_a = agent_id.replace("/", "_").strip() or "default"
    return f"vault/{safe_t}/{safe_a}"


@dataclass
class OAOSContext:
    """pydantic-ai inspired deps_type injected into every tool.

    Mirrors pydantic-ai's deps_type pattern: a typed context object carrying
    tenant/agent isolation, trace, vault namespace, and policy snapshot.
    Every tool receives this as first argument when its signature declares
    a parameter named ctx/context/oaos_context or annotated as OAOSContext.
    """

    tenant_id: str = ""
    agent_id: str = ""
    trace_id: str = ""
    vault_path: str = ""
    policy: dict[str, Any] | Any | None = None
    # optional extras for convenience — not required but useful
    session_id: str = ""
    user_id: str = ""
    request_id: str = ""

    def __post_init__(self) -> None:
        if not self.vault_path and self.tenant_id and self.agent_id:
            self.vault_path = _vault_path_for(self.tenant_id, self.agent_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_headers(self) -> dict[str, str]:
        """Derive propagation headers for gateway/MCP."""
        h: dict[str, str] = {}
        if self.tenant_id:
            h["X-Tenant-Id"] = self.tenant_id
        if self.user_id:
            h["X-User-Id"] = self.user_id
        if self.agent_id:
            h["X-Agent-Id"] = self.agent_id
        if self.session_id:
            h["X-Session-Id"] = self.session_id
        if self.trace_id:
            h["X-Trace-Id"] = self.trace_id
        if self.request_id:
            h["X-Request-Id"] = self.request_id
        if self.vault_path:
            h["X-Vault-Path"] = self.vault_path
        return h

    @classmethod
    def from_session(
        cls,
        session: dict[str, Any] | Any,
        trace_id: str | None = None,
        policy: Any | None = None,
    ) -> "OAOSContext":
        """Build context from a session dict or SessionRecord."""
        if isinstance(session, dict):
            tenant_id = str(session.get("tenant_id", ""))
            agent_id = str(session.get("agent_id", ""))
            sid = str(session.get("session_id", ""))
            uid = str(session.get("user_id", ""))
            tid = str(session.get("trace_id", "") or trace_id or "")
            vp = str(session.get("vault_path", "") or "")
            pol = session.get("policy", policy)
        else:
            tenant_id = str(getattr(session, "tenant_id", ""))
            agent_id = str(getattr(session, "agent_id", ""))
            sid = str(getattr(session, "session_id", ""))
            uid = str(getattr(session, "user_id", ""))
            tid = str(getattr(session, "trace_id", "") or trace_id or "")
            vp = str(getattr(session, "vault_path", "") or "")
            pol = getattr(session, "policy", policy)
        if not vp and tenant_id and agent_id:
            vp = _vault_path_for(tenant_id, agent_id)
        return cls(
            tenant_id=tenant_id,
            agent_id=agent_id,
            trace_id=tid or trace_id or _new_id("trace", 12),
            vault_path=vp,
            policy=pol,
            session_id=sid,
            user_id=uid,
            request_id=str((session.get("request_id", "") if isinstance(session, dict) else getattr(session, "request_id", "")) or ""),
        )


@dataclass
class SessionRecord:
    session_id: str
    tenant_id: str
    agent_id: str
    user_id: str = ""
    trace_id: str = ""
    security_domain: str = "general"
    status: str = "active"  # active | cancelled | completed
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["updated_at"] = self.updated_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionRecord":
        for k in ("created_at", "updated_at"):
            v = data.get(k)
            if isinstance(v, str):
                try:
                    data[k] = datetime.fromisoformat(v)
                except Exception:
                    data[k] = _now()
        # drop unknown keys gracefully
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in allowed}
        return cls(**filtered)  # type: ignore[arg-type]

    def to_oaos_context(self, policy: Any | None = None, trace_id: str | None = None) -> OAOSContext:
        return OAOSContext.from_session(self.to_dict(), trace_id=trace_id, policy=policy)

    def to_agent_context(self, request_id: str = "") -> dict[str, Any]:
        # compat helper
        return {
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "request_id": request_id,
            "vault_path": _vault_path_for(self.tenant_id, self.agent_id),
        }


class _MemoryStore:
    def __init__(self) -> None:
        self._store: dict[str, SessionRecord] = {}

    def save(self, rec: SessionRecord) -> None:
        self._store[rec.session_id] = rec

    def load(self, sid: str) -> SessionRecord | None:
        return self._store.get(sid)

    def delete(self, sid: str) -> None:
        self._store.pop(sid, None)

    def all_ids(self) -> list[str]:
        return list(self._store.keys())


class _RedisStore:
    """Optional Redis store — used only if configured and redis is installed."""

    def __init__(self, redis_url: str | None = None, prefix: str = "oaos:agent-runtime:session:", ttl: int = 86400, fallback: bool = True) -> None:
        self._prefix = prefix
        self._ttl = ttl
        self._fallback = _MemoryStore() if fallback else None
        self._client = None
        url = redis_url or os.getenv("OAOS_SESSION_REDIS_URL") or os.getenv("REDIS_URL") or os.getenv("OAOS_CP_REDIS_URL") or "redis://localhost:6379/0"
        try:
            import redis as redis_lib  # type: ignore

            self._client = redis_lib.Redis.from_url(url, decode_responses=True)
            self._client.ping()
            self._url = url
        except Exception:
            if self._fallback is None:
                raise
            self._client = None

    def _key(self, sid: str) -> str:
        return f"{self._prefix}{sid}"

    def save(self, rec: SessionRecord) -> None:
        if self._client is None:
            assert self._fallback is not None
            self._fallback.save(rec)
            return
        self._client.set(self._key(rec.session_id), json.dumps(rec.to_dict(), ensure_ascii=False), ex=self._ttl)

    def load(self, sid: str) -> SessionRecord | None:
        if self._client is None:
            assert self._fallback is not None
            return self._fallback.load(sid)
        raw = self._client.get(self._key(sid))
        if not raw:
            return None
        try:
            return SessionRecord.from_dict(json.loads(raw))
        except Exception:
            return None

    def delete(self, sid: str) -> None:
        if self._client is None:
            assert self._fallback is not None
            self._fallback.delete(sid)
            return
        self._client.delete(self._key(sid))


def _choose_store() -> Any:
    backend = (os.getenv("OAOS_SESSION_BACKEND") or os.getenv("OAOS_AGENT_RUNTIME_SESSION_BACKEND") or "memory").lower()
    if backend in ("redis", "openagentos", "postgres"):
        # openagentos → try redis first (hot cache), else memory; DB persistence is out of scope for minimal impl
        try:
            return _RedisStore(fallback=True)
        except Exception:
            return _MemoryStore()
    # also auto-use redis if URL env is set and redis reachable, but keep memory default
    if os.getenv("REDIS_URL") or os.getenv("OAOS_CP_REDIS_URL") or os.getenv("OAOS_SESSION_REDIS_URL"):
        try:
            return _RedisStore(fallback=True)
        except Exception:
            return _MemoryStore()
    return _MemoryStore()


class SessionManager:
    """Tenant+agent isolated session manager — §16C.1.

    Isolation key: (tenant_id, agent_id).  Cross-tenant or cross-agent access
    raises PermissionError.  Missing session raises KeyError.
    """

    def __init__(self, store: Any | None = None) -> None:
        self._store = store or _choose_store()

    # ── internal helpers ──
    def _assert_owner(self, rec: SessionRecord, tenant_id: str, agent_id: str) -> None:
        if rec.tenant_id != tenant_id or rec.agent_id != agent_id:
            raise PermissionError(f"session isolation violation: session belongs to {rec.tenant_id}/{rec.agent_id}, caller {tenant_id}/{agent_id}")

    # ── public API (sync) ──
    def create(
        self,
        tenant_id: str,
        agent_id: str,
        user_id: str = "",
        security_domain: str = "general",
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        trace_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        sid = session_id or _new_id("sess", 16)
        tid = trace_id or _new_id("trace", 16)
        rec = SessionRecord(
            session_id=sid,
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_id or kwargs.get("user_id", ""),
            trace_id=tid,
            security_domain=security_domain,
            metadata=dict(metadata or {}),
        )
        # allow extra metadata via kwargs
        for k in ("request_id",):
            if k in kwargs:
                rec.metadata[k] = kwargs[k]
        self._store.save(rec)
        return rec.to_dict()

    def resume(self, session_id: str, tenant_id: str, agent_id: str) -> dict[str, Any]:
        rec = self._store.load(session_id)
        if rec is None:
            raise KeyError(f"session not found: {session_id}")
        self._assert_owner(rec, tenant_id, agent_id)
        if rec.status == "cancelled":
            raise ValueError(f"session cancelled: {session_id}")
        rec.updated_at = _now()
        self._store.save(rec)
        return rec.to_dict()

    def cancel(self, session_id: str, tenant_id: str, agent_id: str) -> dict[str, Any]:
        rec = self._store.load(session_id)
        if rec is None:
            raise KeyError(f"session not found: {session_id}")
        self._assert_owner(rec, tenant_id, agent_id)
        rec.status = "cancelled"
        rec.updated_at = _now()
        self._store.save(rec)
        return {"status": "cancelled", "session_id": session_id}

    def get_state(self, session_id: str, tenant_id: str, agent_id: str) -> dict[str, Any]:
        rec = self._store.load(session_id)
        if rec is None:
            raise KeyError(f"session not found: {session_id}")
        self._assert_owner(rec, tenant_id, agent_id)
        return rec.to_dict()

    # alias for compat
    def get(self, session_id: str, tenant_id: str, agent_id: str) -> dict[str, Any]:
        return self.get_state(session_id, tenant_id, agent_id)

    def get_oaos_context(self, session_id: str, tenant_id: str, agent_id: str, policy: Any | None = None) -> OAOSContext:
        """Build OAOSContext for the session — injected into every tool."""
        rec = self._store.load(session_id)
        if rec is None:
            raise KeyError(f"session not found: {session_id}")
        self._assert_owner(rec, tenant_id, agent_id)
        return rec.to_oaos_context(policy=policy)

    # ── async wrappers ──
    async def acreate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.create(*args, **kwargs)

    async def aresume(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.resume(*args, **kwargs)

    async def acancel(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.cancel(*args, **kwargs)

    async def aget_state(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.get_state(*args, **kwargs)

    # helpers
    def _get_record(self, session_id: str) -> SessionRecord | None:
        return self._store.load(session_id)
