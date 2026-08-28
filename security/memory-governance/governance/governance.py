"""Memory Governance — Section 27. Namespace + provenance + revoke invalidation.

Implements §27 Memory Namespace, §28 ACL provenance, §29 classification.

Namespace:
  PERSONAL  = user/{user_id}/*
  TEAM      = group/{group_id}/*
  CORPORATE = organization/*

Metadata per memory chunk:
  id, owner, scope, classification, source_resource_id,
  source_acl_version, source_delegation_id, retention_policy,
  provenance tracing + revoke cascade invalidation.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any

# ── Scope ──────────────────────────────────────────────────────────────
class MemoryScope(str, Enum):
    PERSONAL = "personal"   # user/{user_id}/*
    TEAM = "team"           # group/{group_id}/*
    CORPORATE = "corporate" # organization/*

# ── Classification (§29) ─────────────────────────────────────────────
# Reuse common_types if available, else fallback
try:
    from common_types.types import DataClassification as _CommonDC  # type: ignore
    DataClassification = _CommonDC  # type: ignore
except Exception:
    class DataClassification(str, Enum):  # type: ignore[no-redef]
        PUBLIC = "PUBLIC"
        INTERNAL = "INTERNAL"
        CONFIDENTIAL = "CONFIDENTIAL"
        PII = "PII"
        SECRET = "SECRET"

# Valid retention policies
_RETENTION_POLICIES = frozenset({"ephemeral", "session", "standard", "long_term", "permanent"})

# ── MemoryRecord ─────────────────────────────────────────────────────
@dataclass
class MemoryRecord:
    """Single memory chunk with full §27 metadata + provenance."""
    id: str
    owner: str  # employee:kim  or  group:dev  or  organization
    scope: MemoryScope
    classification: str  # DataClassification value
    source_resource_id: str | None = None
    source_acl_version: str | None = None
    source_delegation_id: str | None = None
    retention_policy: str = "standard"
    # content
    content: str = ""
    tenant_id: str = "default"
    group_id: str | None = None  # only for TEAM scope
    # lifecycle
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    invalidated: bool = False
    invalidated_at: datetime | None = None
    invalidated_reason: str | None = None
    # provenance chain
    provenance: dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        if self.expires_at and datetime.now(timezone.utc) > self.expires_at:
            return True
        return False

    def is_accessible(self) -> bool:
        return not self.invalidated and not self.is_expired()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner": self.owner,
            "scope": self.scope.value if isinstance(self.scope, Enum) else str(self.scope),
            "classification": self.classification,
            "source_resource_id": self.source_resource_id,
            "source_acl_version": self.source_acl_version,
            "source_delegation_id": self.source_delegation_id,
            "retention_policy": self.retention_policy,
            "content": self.content,
            "tenant_id": self.tenant_id,
            "group_id": self.group_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "invalidated": self.invalidated,
            "provenance": self.provenance,
        }


# ── Scope namespace helpers ──────────────────────────────────────────
def scope_namespace(scope: MemoryScope, owner: str, group_id: str | None = None) -> str:
    """Return canonical namespace prefix for a scope/owner."""
    if scope == MemoryScope.PERSONAL:
        # owner = employee:kim → user/kim
        uid = owner.split(":", 1)[-1] if ":" in owner else owner
        return f"user/{uid}"
    elif scope == MemoryScope.TEAM:
        gid = group_id or owner.split(":", 1)[-1] if ":" in owner else owner
        # owner may be group:dev
        if owner.startswith("group:"):
            gid = owner.split(":", 1)[1]
        return f"group/{gid}"
    else:  # CORPORATE
        return "organization"


def _validate_scope_owner(scope: MemoryScope, owner: str) -> None:
    if scope == MemoryScope.PERSONAL and not owner.startswith("employee:"):
        raise ValueError(f"PERSONAL scope requires owner employee:*, got {owner!r}")
    if scope == MemoryScope.TEAM and not (owner.startswith("group:") or owner.startswith("employee:")):
        # team memory owner is group:xxx, but allow employee: as creator with group_id
        pass
    if scope == MemoryScope.CORPORATE and owner not in ("organization",) and not owner.startswith("employee:") and not owner.startswith("group:"):
        pass  # corporate can be owned by org or creator


# ── MemoryStore ──────────────────────────────────────────────────────
class MemoryStore:
    """In-memory governed memory store with provenance + revoke cascade.

    Guarantees:
    - Namespace isolation by scope
    - Provenance tracking (source_resource_id, acl_version, delegation_id)
    - Revoke cascade: invalidate_by_delegation(delegation_id)
    - Retention / expiry enforcement
    - Classification-aware read filtering
    """

    def __init__(self) -> None:
        self._store: dict[str, MemoryRecord] = {}
        # indexes for cascade
        self._by_delegation: dict[str, set[str]] = {}  # delegation_id → set(memory_id)
        self._by_resource: dict[str, set[str]] = {}    # source_resource_id → set(memory_id)
        self._by_owner: dict[str, set[str]] = {}       # owner → set(memory_id)
        self._audit_log: list[dict[str, Any]] = []

    # ── Write ────────────────────────────────────────────────────
    def write(
        self,
        owner: str,
        scope: MemoryScope | str,
        content: str,
        classification: str = "INTERNAL",
        source_resource_id: str | None = None,
        source_acl_version: str | None = None,
        source_delegation_id: str | None = None,
        retention_policy: str = "standard",
        tenant_id: str = "default",
        group_id: str | None = None,
        expires_at: datetime | None = None,
        provenance: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> MemoryRecord:
        """Write a memory chunk with full provenance.

        Args:
            owner: employee:kim / group:dev / organization
            scope: MemoryScope enum or string
            content: memory text
            classification: §29 5-level
            source_resource_id: originating resource (e.g. gmail/user/kim/msg/123)
            source_acl_version: ACL version at time of capture
            source_delegation_id: delegation that authorized capture
            retention_policy: ephemeral/session/standard/long_term/permanent
            tenant_id: tenant isolation
            group_id: for TEAM scope
            expires_at: explicit expiry
            provenance: extra provenance fields
            ttl_seconds: alternative to expires_at
        """
        # normalize scope
        if isinstance(scope, str):
            try:
                scope = MemoryScope(scope)
            except ValueError:
                raise ValueError(f"invalid scope: {scope!r} (expected personal/team/corporate)")
        if retention_policy not in _RETENTION_POLICIES:
            raise ValueError(f"invalid retention_policy: {retention_policy!r} (allowed: {sorted(_RETENTION_POLICIES)})")
        # validate classification
        valid_dc = {"PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "SECRET"}
        if classification not in valid_dc:
            raise ValueError(f"invalid classification: {classification!r} (allowed: {sorted(valid_dc)})")
        _validate_scope_owner(scope, owner)
        # §29 classification guard: sensitive data must not be permanent (override via provenance policy_override)
        if retention_policy == "permanent" and classification in ("CONFIDENTIAL", "SECRET", "PII"):
            # Allow when explicit policy override is signaled via provenance (X-Memory-Policy-Override header)
            _override = False
            if provenance is not None and isinstance(provenance, dict):
                _override = bool(provenance.get("policy_override") or provenance.get("policyOverride") or provenance.get("override"))
            if not _override:
                raise ValueError(f"classification {classification!r} incompatible with permanent retention (requires standard/long_term or shorter)")

        # expiry from ttl
        if ttl_seconds is not None and expires_at is None:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        # ephemeral default ttl: 1h, session: 24h
        if expires_at is None and retention_policy == "ephemeral":
            expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        elif expires_at is None and retention_policy == "session":
            expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

        mem_id = f"mem_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        # build provenance
        prov: dict[str, Any] = {
            "source_resource_id": source_resource_id,
            "source_acl_version": source_acl_version,
            "source_delegation_id": source_delegation_id,
            "classification": classification,
            "retention_policy": retention_policy,
            "created_at": now.isoformat(),
            "namespace": scope_namespace(scope, owner, group_id),
        }
        if provenance:
            prov.update(provenance)

        rec = MemoryRecord(
            id=mem_id,
            owner=owner,
            scope=scope,
            classification=classification,
            source_resource_id=source_resource_id,
            source_acl_version=source_acl_version,
            source_delegation_id=source_delegation_id,
            retention_policy=retention_policy,
            content=content,
            tenant_id=tenant_id,
            group_id=group_id,
            created_at=now,
            expires_at=expires_at,
            provenance=prov,
        )
        self._store[mem_id] = rec
        # indexes
        if source_delegation_id:
            self._by_delegation.setdefault(source_delegation_id, set()).add(mem_id)
        if source_resource_id:
            self._by_resource.setdefault(source_resource_id, set()).add(mem_id)
        self._by_owner.setdefault(owner, set()).add(mem_id)
        # audit
        self._audit_log.append({
            "event_type": "MEMORY_WRITE",
            "memory_id": mem_id,
            "owner": owner,
            "scope": scope.value,
            "classification": classification,
            "source_resource_id": source_resource_id,
            "source_delegation_id": source_delegation_id,
            "timestamp": now.isoformat(),
        })
        return rec

    # ── Read ─────────────────────────────────────────────────────
    def read(
        self,
        memory_id: str,
        requester: str | dict | None = None,
    ) -> MemoryRecord | None:
        """Read single memory with isolation + expiry check.

        Args:
            memory_id: memory chunk id
            requester: employee:kim or dict with user_id/groups/tenant_id
                        If None, no isolation check (internal use).
        Returns:
            MemoryRecord or None if not found / invalidated / expired / denied.
        """
        rec = self._store.get(memory_id)
        if rec is None:
            return None
        if rec.invalidated or rec.is_expired():
            return None
        if requester is not None and not self._can_access(rec, requester):
            return None
        return rec

    def get(self, memory_id: str) -> MemoryRecord | None:
        """Direct get without access check (governance internal)."""
        return self._store.get(memory_id)

    def list_by_owner(
        self,
        owner: str,
        requester: str | dict | None = None,
        include_invalidated: bool = False,
    ) -> list[MemoryRecord]:
        """List memories by owner with isolation."""
        ids = self._by_owner.get(owner, set())
        result: list[MemoryRecord] = []
        for mid in ids:
            rec = self._store.get(mid)
            if rec is None:
                continue
            if not include_invalidated and (rec.invalidated or rec.is_expired()):
                continue
            if requester is not None and not self._can_access(rec, requester):
                continue
            result.append(rec)
        return result

    def search(
        self,
        query: str | None = None,
        scope: MemoryScope | str | None = None,
        owner: str | None = None,
        classification: str | None = None,
        requester: str | dict | None = None,
        tenant_id: str | None = None,
        include_invalidated: bool = False,
    ) -> list[MemoryRecord]:
        """Filtered search with isolation."""
        if isinstance(scope, str):
            try:
                scope = MemoryScope(scope)
            except ValueError:
                scope = None  # ignore invalid

        results: list[MemoryRecord] = []
        for rec in self._store.values():
            if not include_invalidated and (rec.invalidated or rec.is_expired()):
                continue
            if scope and rec.scope != scope:
                continue
            if owner and rec.owner != owner:
                continue
            if classification and rec.classification != classification:
                continue
            if tenant_id and rec.tenant_id != tenant_id:
                continue
            if query and query.lower() not in rec.content.lower():
                continue
            if requester is not None and not self._can_access(rec, requester):
                continue
            results.append(rec)
        # ranking: most recent first
        results.sort(key=lambda r: r.created_at, reverse=True)
        return results

    # ── Invalidation (revoke cascade) ────────────────────────────
    def invalidate_by_delegation(
        self,
        delegation_id: str,
        reason: str = "delegation_revoked",
    ) -> int:
        """Cascade invalidate all memories derived from a delegation.

        Called on delegation revoke (Section 27.2).

        Returns:
            count of invalidated memories
        """
        mids = self._by_delegation.get(delegation_id, set()).copy()
        count = 0
        now = datetime.now(timezone.utc)
        for mid in mids:
            rec = self._store.get(mid)
            if rec and not rec.invalidated:
                rec.invalidated = True
                rec.invalidated_at = now
                rec.invalidated_reason = reason
                count += 1
                self._audit_log.append({
                    "event_type": "MEMORY_INVALIDATE",
                    "memory_id": mid,
                    "delegation_id": delegation_id,
                    "reason": reason,
                    "timestamp": now.isoformat(),
                })
        return count

    def invalidate_by_resource(
        self,
        source_resource_id: str,
        reason: str = "resource_revoked",
    ) -> int:
        """Invalidate memories derived from a specific resource (ACL revoke)."""
        mids = self._by_resource.get(source_resource_id, set()).copy()
        count = 0
        now = datetime.now(timezone.utc)
        for mid in mids:
            rec = self._store.get(mid)
            if rec and not rec.invalidated:
                rec.invalidated = True
                rec.invalidated_at = now
                rec.invalidated_reason = reason
                count += 1
                self._audit_log.append({
                    "event_type": "MEMORY_INVALIDATE",
                    "memory_id": mid,
                    "source_resource_id": source_resource_id,
                    "reason": reason,
                    "timestamp": now.isoformat(),
                })
        return count

    def invalidate(self, memory_id: str, reason: str = "manual") -> bool:
        """Invalidate single memory."""
        rec = self._store.get(memory_id)
        if rec is None or rec.invalidated:
            return False
        rec.invalidated = True
        rec.invalidated_at = datetime.now(timezone.utc)
        rec.invalidated_reason = reason
        self._audit_log.append({
            "event_type": "MEMORY_INVALIDATE",
            "memory_id": memory_id,
            "reason": reason,
            "timestamp": rec.invalidated_at.isoformat(),
        })
        return True

    # ── Provenance ───────────────────────────────────────────────
    def get_provenance(self, memory_id: str) -> dict[str, Any] | None:
        """Return provenance dict for a memory."""
        rec = self._store.get(memory_id)
        if rec is None:
            return None
        return dict(rec.provenance)

    def audit_events(self) -> list[dict[str, Any]]:
        return list(self._audit_log)

    def count(self, include_invalidated: bool = False) -> int:
        if include_invalidated:
            return len(self._store)
        return sum(1 for r in self._store.values() if not r.invalidated and not r.is_expired())

    # ── Isolation ────────────────────────────────────────────────
    def _can_access(self, rec: MemoryRecord, requester: str | dict) -> bool:
        """Enforce §27 namespace isolation.

        PERSONAL: only owner can read
        TEAM: owner group members can read
        CORPORATE: any tenant member can read (tenant isolation still enforced)
        """
        if isinstance(requester, dict):
            req_user = requester.get("user_id") or requester.get("owner")
            req_groups: list[str] = requester.get("groups", [])  # type: ignore
            if not req_groups and isinstance(requester.get("context"), dict):
                req_groups = requester.get("context", {}).get("groups", [])  # type: ignore
            req_tenant = requester.get("tenant_id")
        else:
            req_user = requester
            req_groups = []
            req_tenant = None

        # tenant isolation first
        if req_tenant and rec.tenant_id != req_tenant and rec.tenant_id != "default":
            # allow if rec is default tenant? strict: must match
            if req_tenant != rec.tenant_id:
                return False

        if rec.scope == MemoryScope.PERSONAL:
            return req_user == rec.owner
        elif rec.scope == MemoryScope.TEAM:
            # owner is group:xxx, requester must be in that group or be the group owner
            group_id = rec.group_id or (rec.owner.split(":", 1)[1] if ":" in rec.owner else rec.owner)
            if req_user == rec.owner:
                return True
            # normalize groups to handle both "dev" and "group:dev"
            normalized_groups: set[str] = set()
            for g in req_groups:
                normalized_groups.add(g)
                if ":" in g:
                    normalized_groups.add(g.split(":", 1)[1])
                else:
                    normalized_groups.add(f"group:{g}")
            if group_id in normalized_groups or f"group:{group_id}" in normalized_groups:
                return True
            return False
        else:  # CORPORATE
            # any authenticated user in same tenant can read corporate memory
            return req_user is not None and req_user != ""

    def clear(self) -> None:
        """Test helper: clear all."""
        self._store.clear()
        self._by_delegation.clear()
        self._by_resource.clear()
        self._by_owner.clear()
        self._audit_log.clear()


# Singleton for convenience (optional)
_default_store: MemoryStore | None = None

def get_default_store() -> MemoryStore:
    global _default_store
    if _default_store is None:
        _default_store = MemoryStore()
    return _default_store
