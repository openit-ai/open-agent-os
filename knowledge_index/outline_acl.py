"""Outline collection ACL resolution for OAOS Knowledge Index (read-only).

Maps Outline collection sharing semantics onto SourceDocument ACL so the
persistent index never serves a private collection as tenant-public:

- collection ``permission`` in ``{read, read_write}``  -> tenant-public
  (``acl == {}``, single null group/agent entry at persist time).
- collection ``permission`` ``admin`` or ``null``/missing -> members-only:
  explicit agent principals derived deterministically from the Outline
  workspace users' email local-parts. Only active users with a valid email
  are mapped; suspended/deleted/no-email users are skipped (counted in
  provenance, never in ACL).
- ACL resolution failure -> fail-closed: affected documents get a sentinel
  restricted ACL (``agent:__outline_acl_unresolved__``, matches nobody) and
  are NEVER marked public. Callers may choose ``on_error="passthrough"``
  (dev/test only) to keep the source ACL with an ``unresolved`` provenance
  flag; ``"auto"`` (default) is strict in production, passthrough elsewhere.

Read-only: uses only ``collections.info/list``, ``collections.memberships``
and ``users.list`` through the existing adapter transport
(``_post_with_retries``). Never calls create/update/delete and never emits
secrets (tokens/headers) into provenance, logs, or errors.

Source ACL is authoritative: explicit document-level ``groups`` are preserved
(unioned); collection resolution supplies the ``users`` allow-list for private
collections and the ``tenant-public`` verdict for shared collections.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from .models import SourceDocument

__all__ = [
    "PUBLIC_COLLECTION_PERMISSIONS",
    "UNRESOLVED_AGENT_SENTINEL",
    "OutlineACLResolutionError",
    "OutlineCollectionACL",
    "OutlineACLResolver",
    "agent_principal_for_email",
    "is_valid_email",
    "collection_id_for_resource",
    "enrich_source_documents_with_outline_acl",
]


PUBLIC_COLLECTION_PERMISSIONS = frozenset({"read", "read_write"})

UNRESOLVED_AGENT_SENTINEL = "agent:__outline_acl_unresolved__"

_VALID_ON_ERROR = ("auto", "strict", "passthrough")


class OutlineACLResolutionError(RuntimeError):
    """Outline ACL metadata fetch failed (collection/membership/user API)."""


def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        if os.environ.get(k, "").strip().lower() in ("production", "prod"):
            return True
    return False


def is_valid_email(email: Any) -> bool:
    """Permissive-but-safe email check: local@domain, no whitespace."""
    if not isinstance(email, str):
        return False
    e = email.strip()
    if not e or len(e) > 254 or " " in e or "\t" in e or "\n" in e:
        return False
    if e.count("@") != 1:
        return False
    local, domain = e.split("@", 1)
    if not local or not domain:
        return False
    if "." not in domain:
        return False
    if local.startswith(".") or local.endswith(".") or domain.startswith(".") or domain.endswith("."):
        return False
    return True


def agent_principal_for_email(email: Any) -> str | None:
    """Deterministic agent principal for an Outline user email.

    ``"Alice@Example.com"`` -> ``"agent:assistant:alice"`` (local-part, lowercased).
    Returns ``None`` when the email is missing/invalid — the caller must skip
    such users (never synthesize a principal). Callers performing retrieval
    must map their verified identity the same way (email local-part,
    lowercased, ``agent:`` prefix) to match private-collection entries.
    """
    if not is_valid_email(email):
        return None
    local = str(email).strip().split("@", 1)[0].strip().lower()
    if not local:
        return None
    # OAOS Mattermost identity uses agent:assistant:<employee>.
    return f"agent:assistant:{local}"


def collection_id_for_resource(resource_id: Any) -> str:
    """Extract the Outline collection id from ``outline/{collection}/{doc}``."""
    try:
        parts = str(resource_id or "").split("/")
        if len(parts) >= 3 and parts[0] == "outline" and parts[1].strip():
            return parts[1].strip()
    except Exception:
        pass
    return ""


def _normalize_permission(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    return s or None


def _short_hash(*parts: str) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _extract_data_list(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Outline's collections.memberships response wraps the list as
            # {data: {memberships: [...]}, users: [...]}; users.list and
            # collections.list normally use data=[...]. Accept both shapes.
            for key in ("memberships", "users", "collections", "results", "items"):
                nested = data.get(key)
                if isinstance(nested, list):
                    return nested
            return [data]
        for key in ("memberships", "users", "collections", "results", "items"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return nested
        return []
    if isinstance(payload, list):
        return payload
    return []


def _user_active(user: dict[str, Any]) -> bool:
    if not isinstance(user, dict):
        return False
    if user.get("isSuspended"):
        return False
    if user.get("deletedAt") is not None:
        return False
    if user.get("isDeleted"):
        return False
    return True


@dataclass
class OutlineCollectionACL:
    collection_id: str
    permission: str | None
    is_public: bool
    member_agent_ids: list[str] = field(default_factory=list)
    member_user_ids: list[str] = field(default_factory=list)
    member_emails: list[str] = field(default_factory=list)
    mode: str = "members-only"  # tenant-public | members-only | unresolved-restricted
    unresolved: bool = False
    error: str | None = None


class OutlineACLResolver:
    """Read-only Outline collection ACL resolver reusing an adapter transport.

    Args:
        adapter: object exposing ``_post_with_retries(path, body)``
            (e.g. :class:`HttpOutlineSourceAdapter` with an injected
            fake transport in tests). Only read endpoints are ever called.
        page_limit: page size for membership/user listing (1..100).
        max_pages: bound on pages per listing call.
    """

    def __init__(self, adapter: Any, *, page_limit: int = 50, max_pages: int = 50) -> None:
        if adapter is None or not hasattr(adapter, "_post_with_retries"):
            raise ValueError("adapter with _post_with_retries is required (read-only Outline client)")
        self._adapter = adapter
        self.page_limit = max(1, min(int(page_limit), 100))
        self.max_pages = max(1, int(max_pages))
        self._collection_cache: dict[str, dict[str, Any]] = {}
        self._acl_cache: dict[str, OutlineCollectionACL] = {}
        self._users_cache: list[dict[str, Any]] | None = None
        self.calls: list[dict[str, Any]] = []  # audit trail of read paths (no secrets)

    def clear_cache(self) -> None:
        self._collection_cache.clear()
        self._acl_cache.clear()
        self._users_cache = None

    # -- low-level paginated reads -------------------------------------
    def _post(self, path: str, body: dict[str, Any]) -> Any:
        self.calls.append({"path": path})
        resp = self._adapter._post_with_retries(path, body)
        return resp.json() if hasattr(resp, "json") else resp

    def _list_paginated(self, path: str, body_base: dict[str, Any]) -> list[Any]:
        out: list[Any] = []
        offset = 0
        for _ in range(self.max_pages):
            body = dict(body_base)
            body.setdefault("limit", self.page_limit)
            body.setdefault("offset", offset)
            payload = self._post(path, body)
            items = _extract_data_list(payload)
            if not items:
                break
            out.extend(items)
            # pagination bookkeeping: stop when the server reports total
            try:
                pagination = payload.get("pagination") or {} if isinstance(payload, dict) else {}
                total = pagination.get("total")
                if isinstance(total, int) and (offset + len(items)) >= total:
                    break
            except Exception:
                pass
            if len(items) < self.page_limit:
                break
            offset += len(items)
        return out

    # -- collection metadata -------------------------------------------
    def list_collections(self) -> list[dict[str, Any]]:
        items = self._list_paginated("/api/collections.list", {})
        return [i for i in items if isinstance(i, dict)]

    def get_collection(self, collection_id: str) -> dict[str, Any]:
        cid = (collection_id or "").strip()
        if not cid:
            raise OutlineACLResolutionError("collection_id is required")
        if cid in self._collection_cache:
            return self._collection_cache[cid]
        payload = self._post("/api/collections.info", {"id": cid})
        data: Any = None
        if isinstance(payload, dict):
            data = payload.get("data")
            if data is None and payload.get("id"):
                data = payload
        if not isinstance(data, dict) or not data.get("id"):
            raise OutlineACLResolutionError(f"collections.info missing collection for {cid!r}")
        self._collection_cache[cid] = data
        return data

    def _permission_for(self, collection_id: str) -> str | None:
        # Prefer direct info; fall back to list scan (both read-only).
        try:
            info = self.get_collection(collection_id)
            return _normalize_permission(info.get("permission"))
        except OutlineACLResolutionError:
            raise
        except Exception as exc:
            raise OutlineACLResolutionError(f"collections.info failed: {type(exc).__name__}") from exc

    def _permission_for_with_list_fallback(self, collection_id: str) -> str | None:
        try:
            return self._permission_for(collection_id)
        except OutlineACLResolutionError:
            for coll in self.list_collections():
                if str(coll.get("id") or "").strip() == collection_id:
                    return _normalize_permission(coll.get("permission"))
            raise

    # -- memberships + users -------------------------------------------
    def list_collection_memberships(self, collection_id: str) -> list[dict[str, Any]]:
        cid = (collection_id or "").strip()
        if not cid:
            raise OutlineACLResolutionError("collection_id is required")
        items = self._list_paginated("/api/collections.memberships", {"id": cid})
        return [i for i in items if isinstance(i, dict)]

    def list_users(self) -> list[dict[str, Any]]:
        if self._users_cache is not None:
            return self._users_cache
        items = self._list_paginated("/api/users.list", {})
        users = [i for i in items if isinstance(i, dict)]
        self._users_cache = users
        return users

    # -- resolution ------------------------------------------------------
    def resolve_collection(self, collection_id: str) -> OutlineCollectionACL:
        cid = (collection_id or "").strip()
        if not cid:
            return OutlineCollectionACL(
                collection_id="",
                permission=None,
                is_public=False,
                member_agent_ids=[UNRESOLVED_AGENT_SENTINEL],
                mode="unresolved-restricted",
                unresolved=True,
                error="missing collection_id",
            )
        if cid in self._acl_cache:
            return self._acl_cache[cid]
        try:
            permission = self._permission_for_with_list_fallback(cid)
        except Exception as exc:
            acl = OutlineCollectionACL(
                collection_id=cid,
                permission=None,
                is_public=False,
                member_agent_ids=[UNRESOLVED_AGENT_SENTINEL],
                mode="unresolved-restricted",
                unresolved=True,
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
            self._acl_cache[cid] = acl
            return acl
        if permission in PUBLIC_COLLECTION_PERMISSIONS:
            acl = OutlineCollectionACL(
                collection_id=cid, permission=permission, is_public=True, mode="tenant-public"
            )
            self._acl_cache[cid] = acl
            return acl
        # admin / null / unknown -> explicit active-members only
        try:
            acl = self._members_only_acl(cid, permission)
        except Exception as exc:
            acl = OutlineCollectionACL(
                collection_id=cid,
                permission=permission,
                is_public=False,
                member_agent_ids=[UNRESOLVED_AGENT_SENTINEL],
                mode="unresolved-restricted",
                unresolved=True,
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
        self._acl_cache[cid] = acl
        return acl

    def _members_only_acl(self, cid: str, permission: str | None) -> OutlineCollectionACL:
        memberships = self.list_collection_memberships(cid)
        users = self.list_users()
        active_by_id: dict[str, dict[str, Any]] = {}
        for u in users:
            uid = str(u.get("id") or "").strip()
            if uid and _user_active(u):
                active_by_id[uid] = u
        agent_ids: list[str] = []
        member_uids: list[str] = []
        member_emails: list[str] = []
        seen_agents: set[str] = set()
        for m in memberships:
            if not isinstance(m, dict):
                continue
            embedded = m.get("user") if isinstance(m.get("user"), dict) else None
            uid = str(m.get("userId") or m.get("user_id") or (embedded or {}).get("id") or "").strip()
            if not uid:
                continue
            candidate = active_by_id.get(uid)
            email: Any = None
            if candidate is not None:
                email = candidate.get("email")
            elif embedded is not None and _user_active(embedded):
                # Fall back to the membership-embedded user record.
                email = embedded.get("email")
            else:
                continue  # unknown/suspended/deleted -> never in ACL
            principal = agent_principal_for_email(email)
            if principal is None:
                continue  # no valid email -> skip (counted via provenance size diff)
            if principal not in seen_agents:
                seen_agents.add(principal)
                agent_ids.append(principal)
                member_uids.append(uid)
                member_emails.append(str(email).strip())
        agent_ids.sort()
        if not agent_ids:
            return OutlineCollectionACL(
                collection_id=cid,
                permission=permission,
                is_public=False,
                member_agent_ids=[UNRESOLVED_AGENT_SENTINEL],
                member_user_ids=[],
                member_emails=[],
                mode="unresolved-restricted",
                unresolved=True,
                error="no mappable active members with valid email",
            )
        return OutlineCollectionACL(
            collection_id=cid,
            permission=permission,
            is_public=False,
            member_agent_ids=agent_ids,
            member_user_ids=member_uids,
            member_emails=member_emails,
            mode="members-only",
        )

    # -- document enrichment ---------------------------------------------
    def enrich_documents(
        self, docs: list[SourceDocument], *, on_error: str = "auto"
    ) -> tuple[list[SourceDocument], dict[str, dict[str, Any]]]:
        """Enrich SourceDocument ACLs from Outline collection metadata.

        Returns ``(enriched_docs, provenance_by_resource_id)``. Never raises
        for per-document failures: failures become sentinel-restricted
        (``strict``) or keep the source ACL with an ``unresolved`` flag
        (``passthrough``). ``"auto"`` selects strict in production.
        """
        if on_error not in _VALID_ON_ERROR:
            raise ValueError(f"on_error must be one of {_VALID_ON_ERROR}")
        strict = on_error == "strict" or (on_error == "auto" and _is_production())
        enriched: list[SourceDocument] = []
        provenance: dict[str, dict[str, Any]] = {}
        for doc in docs:
            if not isinstance(doc, SourceDocument):
                continue
            try:
                new_doc, prov = self._enrich_one(doc, strict=strict)
            except Exception as exc:
                new_doc, prov = _restrict_doc(doc, f"{type(exc).__name__}: {str(exc)[:200]}")
                if not strict:
                    new_doc, prov = _passthrough_doc(doc, f"{type(exc).__name__}: {str(exc)[:200]}")
            enriched.append(new_doc)
            try:
                provenance[new_doc.resource_id] = prov
            except Exception:
                continue
        return enriched, provenance

    def _enrich_one(
        self, doc: SourceDocument, *, strict: bool
    ) -> tuple[SourceDocument, dict[str, Any]]:
        cid = collection_id_for_resource(doc.resource_id)
        source_acl = dict(doc.acl or {})
        if not cid:
            if strict:
                return _restrict_doc(doc, "missing collection_id", source_acl=source_acl)
            return _passthrough_doc(doc, "missing collection_id", source_acl=source_acl)
        acl = self.resolve_collection(cid)
        base_prov: dict[str, Any] = {
            "outline_collection_id": cid,
            "outline_collection_permission": acl.permission,
            "outline_acl_mode": acl.mode,
            "outline_member_user_ids": list(acl.member_user_ids),
            "outline_member_emails": list(acl.member_emails),
            "outline_member_count": len(acl.member_agent_ids) if not acl.unresolved else len(acl.member_user_ids),
            "outline_acl_unresolved": bool(acl.unresolved),
            "outline_source_acl": dict(source_acl),
        }
        if acl.error:
            base_prov["outline_acl_error"] = str(acl.error)[:300]
        if acl.unresolved:
            if strict:
                new_doc, prov = _restrict_doc(doc, acl.error or "unresolved", source_acl=source_acl)
                prov.update({k: v for k, v in base_prov.items() if k not in prov})
                return new_doc, prov
            return _passthrough_doc(doc, acl.error or "unresolved", source_acl=source_acl)
        if acl.is_public:
            mhash = _short_hash(acl.permission or "", cid)
            new_doc = SourceDocument(
                resource_id=doc.resource_id,
                source_system=doc.source_system,
                title=doc.title,
                content=doc.content,
                source_updated_at=doc.source_updated_at,
                acl_version=f"{doc.acl_version}~cac:{acl.permission}:{mhash}",
                acl={},
                source_uri=doc.source_uri,
                tenant_id=doc.tenant_id,
                classification=doc.classification,
                content_hash=doc.content_hash,
            )
            return new_doc, base_prov
        # members-only: preserve explicit doc-level groups, supply users.
        groups = list(
            (source_acl.get("groups") or source_acl.get("allowedGroups") or source_acl.get("allowed_groups") or [])
        )
        existing_users = list(
            (source_acl.get("users") or source_acl.get("allowedUsers") or source_acl.get("allowed_users") or [])
        )
        users: list[str] = list(acl.member_agent_ids)
        for u in existing_users:
            su = str(u or "").strip()
            if su and su not in users:
                users.append(su)
        users.sort()
        mhash = _short_hash(acl.permission or "private", ",".join(users))
        merged_acl: dict[str, Any] = {"users": users}
        clean_groups = [str(g).strip() for g in groups if str(g or "").strip()]
        if clean_groups:
            merged_acl["groups"] = sorted(set(clean_groups))
        new_doc = SourceDocument(
            resource_id=doc.resource_id,
            source_system=doc.source_system,
            title=doc.title,
            content=doc.content,
            source_updated_at=doc.source_updated_at,
            acl_version=f"{doc.acl_version}~cac:{acl.permission or 'private'}:{mhash}",
            acl=merged_acl,
            source_uri=doc.source_uri,
            tenant_id=doc.tenant_id,
            classification=doc.classification,
            content_hash=doc.content_hash,
        )
        return new_doc, base_prov


def _restrict_doc(
    doc: SourceDocument, error: str, *, source_acl: dict[str, Any] | None = None
) -> tuple[SourceDocument, dict[str, Any]]:
    """Fail-closed copy: sentinel user ACL that matches nobody (never public)."""
    src = dict(source_acl) if source_acl is not None else dict(doc.acl or {})
    groups = src.get("groups") or src.get("allowedGroups") or src.get("allowed_groups") or []
    clean_groups = [str(g).strip() for g in (groups or []) if str(g or "").strip()]
    acl: dict[str, Any] = {"users": [UNRESOLVED_AGENT_SENTINEL]}
    if clean_groups:
        acl["groups"] = sorted(set(clean_groups))
    new_doc = SourceDocument(
        resource_id=doc.resource_id,
        source_system=doc.source_system,
        title=doc.title,
        content=doc.content,
        source_updated_at=doc.source_updated_at,
        acl_version=f"{doc.acl_version}~cac:unresolved:{_short_hash(doc.resource_id)}",
        acl=acl,
        source_uri=doc.source_uri,
        tenant_id=doc.tenant_id,
        classification=doc.classification,
        content_hash=doc.content_hash,
    )
    prov: dict[str, Any] = {
        "outline_collection_id": collection_id_for_resource(doc.resource_id),
        "outline_acl_mode": "unresolved-restricted",
        "outline_acl_unresolved": True,
        "outline_acl_error": str(error)[:300],
        "outline_member_user_ids": [],
        "outline_member_emails": [],
        "outline_member_count": 0,
        "outline_source_acl": src,
    }
    return new_doc, prov


def _passthrough_doc(
    doc: SourceDocument, error: str, *, source_acl: dict[str, Any] | None = None
) -> tuple[SourceDocument, dict[str, Any]]:
    """Dev/test fallback: keep source ACL, flag unresolved (never in production)."""
    src = dict(source_acl) if source_acl is not None else dict(doc.acl or {})
    prov: dict[str, Any] = {
        "outline_collection_id": collection_id_for_resource(doc.resource_id),
        "outline_acl_mode": "passthrough-unresolved",
        "outline_acl_unresolved": True,
        "outline_acl_error": str(error)[:300],
        "outline_member_user_ids": [],
        "outline_member_emails": [],
        "outline_member_count": 0,
        "outline_source_acl": src,
    }
    return doc, prov


def enrich_source_documents_with_outline_acl(
    docs: list[SourceDocument], adapter: Any, *, on_error: str = "auto"
) -> tuple[list[SourceDocument], dict[str, dict[str, Any]]]:
    """One-shot helper: build a resolver from ``adapter`` and enrich ``docs``."""
    return OutlineACLResolver(adapter).enrich_documents(docs, on_error=on_error)
