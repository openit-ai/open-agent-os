"""IAM adapter — identity provider sync (Google Workspace / Azure Entra / generic OIDC).

Production-grade adapter implementing:
- Provider abstraction (§14 identity, §18 security_domain)
- User/group sync with local cache + httpx delegation to real Directory APIs
- tenant_id / employee: principal mapping (§14), security_domain assignment (§18)
- group → policy bundle binding (GROUP_GRANT §25)
- JIT group sync + Policy Engine integration (Explicit Deny > ... > Default Deny §25)
- Deprovision → revoke delegations + audit ledger (§9, §30-31)

Env:
  IAM_PROVIDER (google|azure|entra|microsoft|okta|oidc|generic)
  IAM_DOMAIN (e.g. example.com)
  IAM_API_KEY / IAM_CREDENTIALS_JSON
  IAM_TENANT_ID (optional explicit)
  IAM_DEFAULT_SECURITY_DOMAIN (default: general)
"""
from __future__ import annotations

import os
import re
import sys
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure security + packages on path even when imported outside pytest conftest
_ROOT = Path(__file__).resolve().parents[2]
for _p in [
    _ROOT / "security" / "policy-engine",
    _ROOT / "security" / "audit",
    _ROOT / "security" / "delegation",
    _ROOT / "security" / "approval",
    _ROOT / "security" / "token",
    _ROOT / "packages" / "policy-model",
    _ROOT / "packages" / "audit-model",
    _ROOT / "packages" / "delegation-model",
    _ROOT / "packages" / "common-types",
    _ROOT / "packages" / "agent-context",
    _ROOT / "security",
    _ROOT / "control-plane",
]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore

# Policy / Audit integration (optional imports — fail gracefully for skeleton)
try:
    from policy_model import PolicyBundle, PolicyDecision, PolicyEvaluationRequest, PolicyEvaluationResult, PolicyRule, PolicySource, POLICY_EVALUATION_ORDER
    from policy_engine.engine import PolicyEngine
    from policy_engine.default_bundle import default_bundle
    _has_policy = True
except Exception:
    _has_policy = False  # type: ignore
    PolicyBundle = Any  # type: ignore
    PolicyEngine = Any  # type: ignore
    def default_bundle(tenant_id="default"):  # type: ignore
        return None

try:
    from audit_model import AuditEvent, AuditEventType
    from audit_ledger.ledger import AuditLedger  # type: ignore
    _has_audit = True
except Exception:
    try:
        from audit_ledger.ledger import AuditLedger  # type: ignore
        from audit_model import AuditEvent, AuditEventType  # type: ignore
        _has_audit = True
    except Exception:
        _has_audit = False  # type: ignore
        AuditEvent = Any  # type: ignore
        AuditEventType = Any  # type: ignore

try:
    from delegation_model import DelegationStatus
    from delegation_service.service import DelegationService  # type: ignore
    _has_delegation = True
except Exception:
    try:
        from delegation_service.service import DelegationService  # type: ignore
        _has_delegation = False
    except Exception:
        _has_delegation = False  # type: ignore


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------

class BaseIamProvider(ABC):
    """Abstract IAM provider — concrete providers implement directory endpoints."""

    provider_name: str = "base"

    def __init__(self, domain: str, api_key: str) -> None:
        self.domain = domain
        self.api_key = api_key

    @abstractmethod
    def user_list_url(self) -> str: ...

    @abstractmethod
    def group_list_url(self) -> str: ...

    @abstractmethod
    def normalize_user(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize provider-specific user dict → canonical {id,email,display_name,groups,...}."""
        ...

    @abstractmethod
    def normalize_group(self, raw: dict[str, Any]) -> tuple[str, list[str]]:
        """Normalize group raw → (group_id, members)."""
        ...

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


class GoogleWorkspaceProvider(BaseIamProvider):
    provider_name = "google"

    def user_list_url(self) -> str:
        return "https://admin.googleapis.com/admin/directory/v1/users"

    def group_list_url(self) -> str:
        return "https://admin.googleapis.com/admin/directory/v1/groups"

    def normalize_user(self, raw: dict[str, Any]) -> dict[str, Any]:
        email = raw.get("primaryEmail") or raw.get("email") or raw.get("id") or ""
        uid = raw.get("id") or email
        return {
            "id": uid,
            "email": email,
            "display_name": raw.get("name", {}).get("fullName") if isinstance(raw.get("name"), dict) else raw.get("display_name") or raw.get("name") or "",
            "groups": raw.get("groups") or [],
            "org_unit": raw.get("orgUnitPath") or "",
            "suspended": raw.get("suspended", False),
            "provider": "google",
        }

    def normalize_group(self, raw: dict[str, Any]) -> tuple[str, list[str]]:
        gid = raw.get("id") or raw.get("email") or raw.get("name", "")
        members = raw.get("members") or raw.get("member_ids") or []
        if isinstance(members, str):
            members = [members]
        return gid, members


class EntraProvider(BaseIamProvider):
    provider_name = "entra"

    def user_list_url(self) -> str:
        return "https://graph.microsoft.com/v1.0/users"

    def group_list_url(self) -> str:
        return "https://graph.microsoft.com/v1.0/groups"

    def normalize_user(self, raw: dict[str, Any]) -> dict[str, Any]:
        email = raw.get("mail") or raw.get("userPrincipalName") or raw.get("email") or raw.get("id") or ""
        uid = raw.get("id") or email
        return {
            "id": uid,
            "email": email,
            "display_name": raw.get("displayName") or raw.get("display_name") or "",
            "groups": raw.get("groups") or raw.get("memberOf") or [],
            "department": raw.get("department") or "",
            "account_enabled": raw.get("accountEnabled", True),
            "provider": "entra",
        }

    def normalize_group(self, raw: dict[str, Any]) -> tuple[str, list[str]]:
        gid = raw.get("id") or raw.get("displayName") or ""
        members = raw.get("members") or raw.get("member_ids") or []
        if isinstance(members, str):
            members = [members]
        return gid, members


class OidcProvider(BaseIamProvider):
    provider_name = "oidc"

    def user_list_url(self) -> str:
        base = os.getenv("IAM_OIDC_BASE_URL", "https://oidc.example.com")
        return f"{base.rstrip('/')}/users"

    def group_list_url(self) -> str:
        base = os.getenv("IAM_OIDC_BASE_URL", "https://oidc.example.com")
        return f"{base.rstrip('/')}/groups"

    def normalize_user(self, raw: dict[str, Any]) -> dict[str, Any]:
        email = raw.get("email") or raw.get("preferred_username") or raw.get("sub") or raw.get("id") or ""
        uid = raw.get("sub") or raw.get("id") or email
        return {
            "id": uid,
            "email": email,
            "display_name": raw.get("name") or raw.get("display_name") or "",
            "groups": raw.get("groups") or raw.get("memberOf") or [],
            "provider": "oidc",
        }

    def normalize_group(self, raw: dict[str, Any]) -> tuple[str, list[str]]:
        gid = raw.get("id") or raw.get("name") or raw.get("displayName") or ""
        members = raw.get("members") or []
        if isinstance(members, str):
            members = [members]
        return gid, members


_PROVIDER_MAP: dict[str, type[BaseIamProvider]] = {
    "google": GoogleWorkspaceProvider,
    "azure": EntraProvider,
    "entra": EntraProvider,
    "microsoft": EntraProvider,
    "ms": EntraProvider,
    "okta": OidcProvider,
    "oidc": OidcProvider,
    "generic": OidcProvider,
}


def _provider_factory(name: str, domain: str, api_key: str) -> BaseIamProvider:
    key = (name or "google").lower().strip()
    cls = _PROVIDER_MAP.get(key, GoogleWorkspaceProvider)
    return cls(domain=domain, api_key=api_key)


# ---------------------------------------------------------------------------
# IAM Adapter
# ---------------------------------------------------------------------------

class IamAdapter:
    """IAM / directory adapter — user/group sync + principal mapping (§14) + tenant & security_domain (§18)."""

    name = "iam"
    provider = "iam"

    TOOL_ACTION: dict[str, str] = {
        "iam_get_user": "READ",
        "iam_list_users": "SEARCH",
        "iam_get_group": "READ",
        "iam_list_groups": "SEARCH",
        "iam_sync_users": "SYNC",
        "iam_resolve_principal": "READ",
        "iam_deprovision_user": "DELETE",
        "iam_jit_sync": "SYNC",
        "iam_evaluate_policy": "READ",
    }

    def __init__(
        self,
        provider: str | None = None,
        domain: str | None = None,
        api_key: str | None = None,
        tenant_id: str | None = None,
        default_security_domain: str | None = None,
        delegation_service: Any | None = None,
        audit_ledger: Any | None = None,
    ) -> None:
        self.iam_provider = (provider or os.getenv("IAM_PROVIDER") or "google").lower()
        self.domain = domain or os.getenv("IAM_DOMAIN") or ""
        self.api_key = api_key or os.getenv("IAM_API_KEY") or os.getenv("IAM_CREDENTIALS_JSON") or ""
        self.tenant_id = tenant_id or os.getenv("IAM_TENANT_ID") or (self.domain.split(".")[0] if self.domain else "default")
        self.default_security_domain = default_security_domain or os.getenv("IAM_DEFAULT_SECURITY_DOMAIN") or "general"
        self._provider_impl: BaseIamProvider = _provider_factory(self.iam_provider, self.domain, self.api_key)
        self._users: dict[str, dict[str, Any]] = {}
        self._groups: dict[str, list[str]] = {}
        self._user_groups: dict[str, set[str]] = {}
        self._principal_map: dict[str, str] = {}
        self._security_domain_map: dict[str, str] = {}
        self._group_policy_bindings: dict[str, dict[str, Any]] = {}
        self._group_bundles: dict[str, Any] = {}
        self.delegation_service = delegation_service
        self.audit_ledger = audit_ledger
        self._group_domain_rules: dict[str, str] = {
            "eng": "development",
            "engineering": "development",
            "dev": "development",
            "finance": "finance",
            "hr": "hr",
            "admin": "admin",
        }

    @staticmethod
    def _sanitize_suffix(raw: str) -> str:
        s = re.sub(r"[^a-z0-9_.-]", "", raw.lower())
        return s or "unknown"

    def to_employee_principal(self, email: str) -> str:
        if not email:
            return "employee:unknown"
        if email.startswith("employee:"):
            return email
        if "@" in email:
            # Use last @ for sanitization (handles display names with @) and join parts without @
            local = email.rsplit("@", 1)[0].lower()
            # For edge case like "Kim@Open!@example.com", local is "Kim@Open!" -> keep both parts joined
            local = local.replace("@", "")
            suffix = re.sub(r"[^a-z0-9_.-]", "", local) or "unknown"
            return f"employee:{suffix}"
        suffix = re.sub(r"[^a-z0-9_.-]", "", email.lower()) or "unknown"
        return f"employee:{suffix}"

    def to_agent_principal(self, employee_principal: str) -> str:
        if not employee_principal.startswith("employee:"):
            raise ValueError("employee_principal must start with employee:")
        return employee_principal.replace("employee:", "agent:assistant:", 1)

    def resolve_principal(self, email_or_id: str) -> dict[str, str]:
        if email_or_id in self._principal_map:
            emp = self._principal_map[email_or_id]
            return {"employee_principal": emp, "agent_principal": self.to_agent_principal(emp), "provider": self.iam_provider, "tenant_id": self.tenant_id}
        emp = self.to_employee_principal(email_or_id) if ("@" in email_or_id or not email_or_id.startswith("employee:")) else email_or_id
        if not emp.startswith("employee:"):
            emp = self.to_employee_principal(email_or_id)
        return {"employee_principal": emp, "agent_principal": self.to_agent_principal(emp), "provider": self.iam_provider, "tenant_id": self.tenant_id}

    def register_principal(self, external_id: str, employee_principal: str) -> None:
        if not employee_principal.startswith("employee:"):
            raise ValueError("employee_principal must start with employee:")
        self._principal_map[external_id] = employee_principal

    def resolve_tenant(self, email_or_domain: str | None = None) -> str:
        if self.tenant_id and self.tenant_id != "default":
            return self.tenant_id
        if email_or_domain and "@" in email_or_domain:
            dom = email_or_domain.split("@")[-1]
            return dom.split(".")[0] or "default"
        if self.domain:
            return self.domain.split(".")[0]
        return "default"

    def assign_security_domain(self, user: dict[str, Any] | str) -> str:
        if isinstance(user, str):
            user = self._users.get(user, {"email": user, "groups": list(self._user_groups.get(user, set()))})
        if user.get("security_domain"):
            return str(user["security_domain"])
        groups: list[str] = user.get("groups") or []
        uid = user.get("id") or user.get("email") or ""
        if uid in self._user_groups:
            groups = list(set(groups) | self._user_groups[uid])
        for g in groups:
            if g in self._security_domain_map:
                return self._security_domain_map[g]
            g_lower = g.lower()
            for prefix, domain in self._group_domain_rules.items():
                if prefix in g_lower:
                    return domain
        for attr_key in ("org_unit", "department", "ou", "orgUnitPath"):
            val = user.get(attr_key)
            if val:
                vl = str(val).lower()
                for prefix, domain in self._group_domain_rules.items():
                    if prefix in vl:
                        return domain
                if "development" in vl or "/dev" in vl:
                    return "development"
                if "finance" in vl:
                    return "finance"
        return self.default_security_domain

    def configure_security_domain(self, mapping: dict[str, str]) -> None:
        self._security_domain_map.update(mapping)

    def configure_group_domain_rules(self, rules: dict[str, str]) -> None:
        self._group_domain_rules.update({k.lower(): v for k, v in rules.items()})

    def bind_group_policy(self, group_id: str, bundle: Any | None = None, rules: list[Any] | None = None, security_domain: str | None = None) -> dict[str, Any]:
        if bundle is None and rules is None:
            raise ValueError("bundle or rules required")
        if bundle is None and rules is not None:
            if _has_policy:
                try:
                    bundle = PolicyBundle(  # type: ignore
                        id=f"group-bundle-{group_id}",
                        tenant_id=self.tenant_id,
                        name=f"Group {group_id} bundle",
                        version="1.0.0",
                        rules=rules,
                    )
                except Exception:
                    bundle = {"id": f"group-bundle-{group_id}", "rules": rules}
            else:
                bundle = {"id": f"group-bundle-{group_id}", "rules": rules}
        self._group_policy_bindings[group_id] = {"bundle": bundle, "security_domain": security_domain}
        self._group_bundles[group_id] = bundle
        if security_domain:
            self._security_domain_map[group_id] = security_domain
        return {"group_id": group_id, "bundle_id": getattr(bundle, "id", bundle.get("id") if isinstance(bundle, dict) else str(bundle)), "security_domain": security_domain}

    def get_group_bundle(self, group_id: str) -> Any | None:
        return self._group_bundles.get(group_id)

    def resolve_group_bundles(self, groups: list[str]) -> list[Any]:
        bundles: list[Any] = []
        for g in groups:
            b = self._group_bundles.get(g)
            if b:
                bundles.append(b)
        return bundles

    def _headers(self) -> dict[str, str]:
        return self._provider_impl.headers()

    async def get_user(self, user_id: str) -> dict[str, Any]:
        if user_id in self._users:
            u = dict(self._users[user_id])
            email = u.get("email") or user_id
            principal = self.to_employee_principal(email)
            u["employee_principal"] = principal
            u["agent_principal"] = self.to_agent_principal(principal)
            u["tenant_id"] = self.resolve_tenant(email)
            u["security_domain"] = self.assign_security_domain(u)
            return u
        if not self.api_key:
            principal = self.to_employee_principal(user_id)
            return {"_skeleton": True, "user_id": user_id, "principal": principal, "employee_principal": principal, "tenant_id": self.resolve_tenant(user_id), "security_domain": self.default_security_domain, "message": "IAM_API_KEY not set"}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        url = f"{self._provider_impl.user_list_url()}/{user_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            raw = resp.json()
            norm = self._provider_impl.normalize_user(raw)
            self._users[norm["id"]] = norm
            if norm.get("email"):
                self._users[norm["email"]] = norm
            return norm

    async def list_users(self, domain: str | None = None, max_results: int = 100) -> dict[str, Any]:
        if not self.api_key:
            users = list(self._users.values())[:max_results]
            seen: set[str] = set()
            deduped: list[dict[str, Any]] = []
            for u in users:
                uid = u.get("id") or u.get("email") or ""
                if uid not in seen:
                    seen.add(uid)
                    deduped.append(u)
            for u in deduped:
                email = u.get("email") or u.get("id") or ""
                u.setdefault("employee_principal", self.to_employee_principal(email))
                u.setdefault("tenant_id", self.resolve_tenant(email))
                u.setdefault("security_domain", self.assign_security_domain(u))
            return {"_skeleton": True, "users": deduped[:max_results], "count": len(deduped), "message": "IAM_API_KEY not set — returning local cache", "provider": self.iam_provider}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        url = self._provider_impl.user_list_url()
        params: dict[str, Any] = {"maxResults": max_results}
        if domain or self.domain:
            params["domain"] = domain or self.domain
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers(), params=params)
            resp.raise_for_status()
            data = resp.json()
            raw_users = data.get("users") or data.get("value") or data.get("Resources") or []
            normalized = [self._provider_impl.normalize_user(r) for r in raw_users]
            for n in normalized:
                self._users[n["id"]] = n
                if n.get("email"):
                    self._users[n["email"]] = n
            return {"users": normalized[:max_results], "count": len(normalized), "provider": self.iam_provider}

    async def get_group(self, group_id: str) -> dict[str, Any]:
        members = self._groups.get(group_id, [])
        if members:
            return {"group_id": group_id, "members": members, "security_domain": self._security_domain_map.get(group_id, self.default_security_domain), "member_count": len(members)}
        if not self.api_key:
            return {"_skeleton": True, "group_id": group_id, "message": "IAM_API_KEY not set", "provider": self.iam_provider}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        url = f"{self._provider_impl.group_list_url()}/{group_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            raw = resp.json()
            gid, m = self._provider_impl.normalize_group(raw)
            self._groups[gid] = m
            return {"group_id": gid, "members": m}

    async def list_groups(self, domain: str | None = None) -> dict[str, Any]:
        if not self.api_key:
            groups = list(self._groups.keys())
            return {"_skeleton": True, "groups": groups, "member_map": dict(self._groups), "message": "IAM_API_KEY not set", "provider": self.iam_provider}
        if httpx is None:
            raise RuntimeError("httpx not installed")
        url = self._provider_impl.group_list_url()
        params: dict[str, Any] = {}
        if domain or self.domain:
            params["domain"] = domain or self.domain
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers(), params=params)
            resp.raise_for_status()
            data = resp.json()
            raw_groups = data.get("groups") or data.get("value") or []
            for rg in raw_groups:
                gid, m = self._provider_impl.normalize_group(rg)
                self._groups[gid] = m
            return {"groups": list(self._groups.keys()), "provider": self.iam_provider}

    async def sync_users(self, users: list[dict[str, Any]]) -> dict[str, Any]:
        count = 0
        for raw in users:
            try:
                norm = self._provider_impl.normalize_user(raw)
                for k, v in raw.items():
                    if k not in norm or norm[k] in ("", [], None):
                        norm[k] = v
            except Exception:
                norm = raw
            uid = norm.get("id") or norm.get("email") or norm.get("principal", "")
            if not uid:
                continue
            email = norm.get("email") or uid
            norm["employee_principal"] = self.to_employee_principal(email)
            norm["security_domain"] = self.assign_security_domain(norm)
            norm["tenant_id"] = self.resolve_tenant(email)
            self._users[uid] = norm
            if email and email != uid:
                self._users[email] = norm
            groups = norm.get("groups") or []
            self._user_groups[uid] = set(groups)
            if email:
                self._user_groups[email] = set(groups)
            for g in groups:
                if g not in self._groups:
                    self._groups[g] = []
                if email not in self._groups[g] and uid not in self._groups[g]:
                    self._groups[g].append(email or uid)
            count += 1
        deduped_ids = set()
        for u in self._users.values():
            deduped_ids.add(u.get("id") or u.get("email"))
        return {"synced": count, "total": len(deduped_ids), "provider": self.iam_provider}

    async def sync_groups(self, groups: dict[str, list[str]]) -> dict[str, Any]:
        for gid, members in groups.items():
            if gid not in self._groups:
                self._groups[gid] = []
            for m in members:
                if m not in self._groups[gid]:
                    self._groups[gid].append(m)
                if m not in self._user_groups:
                    self._user_groups[m] = set()
                self._user_groups[m].add(gid)
                if "@" in m:
                    princ = self.to_employee_principal(m)
                    if princ not in self._user_groups:
                        self._user_groups[princ] = set()
                    self._user_groups[princ].add(gid)
        return {"synced": len(groups), "total": len(self._groups)}

    async def jit_sync_user_groups(self, user_id: str) -> dict[str, Any]:
        cached_groups = self._user_groups.get(user_id, set())
        email = None
        user = self._users.get(user_id, {})
        if user:
            email = user.get("email")
        if email and email in self._user_groups:
            cached_groups = cached_groups | self._user_groups[email]
        if self.api_key and httpx is not None:
            try:
                if isinstance(self._provider_impl, EntraProvider):
                    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/memberOf"
                elif isinstance(self._provider_impl, GoogleWorkspaceProvider):
                    url = f"https://admin.googleapis.com/admin/directory/v1/groups?userKey={user_id}"
                else:
                    url = f"{self._provider_impl.group_list_url()}?user={user_id}"
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(url, headers=self._headers())
                    if resp.status_code == 200:
                        data = resp.json()
                        raw_groups = data.get("groups") or data.get("value") or []
                        fetched: set[str] = set()
                        for rg in raw_groups:
                            gid, _ = self._provider_impl.normalize_group(rg) if isinstance(rg, dict) else (str(rg), [])
                            fetched.add(gid)
                        for g in fetched:
                            if g not in self._groups:
                                self._groups[g] = []
                            if user_id not in self._groups[g]:
                                self._groups[g].append(user_id)
                        if user_id not in self._user_groups:
                            self._user_groups[user_id] = set()
                        self._user_groups[user_id].update(fetched)
                        cached_groups = cached_groups | fetched
            except Exception:
                pass
        effective_user = self._users.get(user_id, {"id": user_id, "email": email or user_id, "groups": list(cached_groups)})
        effective_user["groups"] = list(cached_groups)
        sec_domain = self.assign_security_domain(effective_user)
        bundles = self.resolve_group_bundles(list(cached_groups))
        return {"user_id": user_id, "groups": sorted(cached_groups), "security_domain": sec_domain, "bundles": [getattr(b, "id", b.get("id") if isinstance(b, dict) else str(b)) for b in bundles], "provider": self.iam_provider}

    def build_policy_bundles_for_user(self, principal: str, groups: list[str] | None = None, tenant_id: str | None = None) -> list[Any]:
        if not _has_policy:
            return []
        tid = tenant_id or self.tenant_id or self.resolve_tenant(principal)
        grp_list = groups or []
        if not grp_list and principal:
            cand_ids = [principal, principal.replace("employee:", ""), principal.replace("employee:", "") + f"@{self.domain}" if self.domain else ""]
            gset: set[str] = set()
            for cid in cand_ids:
                if cid in self._user_groups:
                    gset.update(self._user_groups[cid])
            for gid, members in self._groups.items():
                for m in members:
                    if principal in m or principal.split(":")[-1] in m.lower():
                        gset.add(gid)
            grp_list = list(gset | set(grp_list))
        group_bundles = self.resolve_group_bundles(grp_list)
        d_bundle = default_bundle(tenant_id=tid) if _has_policy else None
        bundles: list[Any] = []
        bundles.extend(group_bundles)
        if d_bundle:
            bundles.append(d_bundle)
        return bundles

    def evaluate_access(
        self,
        principal: str,
        action: str,
        resource: str,
        groups: list[str] | None = None,
        extra_bundles: list[Any] | None = None,
        tenant_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        global _has_policy
        if not _has_policy:
            # try lazy re-import after conftest added paths
            try:
                from policy_model import PolicyBundle as PB, PolicyDecision as PD, PolicyEvaluationRequest as PER, PolicyRule as PR, PolicySource as PS  # type: ignore
                from policy_engine.engine import PolicyEngine as PE  # type: ignore
                from policy_engine.default_bundle import default_bundle as db  # type: ignore
                globals()["PolicyBundle"] = PB
                globals()["PolicyEngine"] = PE
                globals()["default_bundle"] = db
                globals()["PolicyDecision"] = PD
                globals()["PolicyEvaluationRequest"] = PER
                globals()["PolicyRule"] = PR
                globals()["PolicySource"] = PS
                _has_policy = True
            except Exception:
                pass
        if not _has_policy:
            try:
                from policy_model import PolicyDecision as PD, PolicySource as PS  # type: ignore
                class _Res:
                    def __init__(self):
                        self.decision = PD.DENY
                        self.source = PS.DEFAULT_DENY
                        self.reason = "policy engine unavailable"
                        self.matched_rule = None
                return _Res()
            except Exception:
                return {"decision": "DENY", "reason": "no policy engine"}
        tid = tenant_id or self.tenant_id or self.resolve_tenant(principal)
        agent_id = self.to_agent_principal(principal) if principal.startswith("employee:") else f"agent:assistant:{principal}"
        req = PolicyEvaluationRequest(  # type: ignore
            tenant_id=tid,
            user_id=principal,
            agent_id=agent_id,
            action=action,
            resource=resource,
            context=context or {},
        )
        bundles = self.build_policy_bundles_for_user(principal, groups=groups, tenant_id=tid)
        if extra_bundles:
            bundles.extend(extra_bundles)
        if not bundles:
            bundles = [default_bundle(tenant_id=tid)]  # type: ignore
        engine = PolicyEngine(bundles=bundles)  # type: ignore
        return engine.evaluate(req)

    async def deprovision_user(
        self,
        user_id: str,
        delegation_service: Any | None = None,
        audit_ledger: Any | None = None,
        reason: str = "iam_deprovision",
    ) -> dict[str, Any]:
        ds = delegation_service or self.delegation_service
        ledger = audit_ledger or self.audit_ledger
        candidates = [user_id]
        user_obj = self._users.get(user_id)
        if user_obj:
            email = user_obj.get("email")
            if email and email not in candidates:
                candidates.append(email)
            princ = user_obj.get("employee_principal") or self.to_employee_principal(email or user_id)
            if princ not in candidates:
                candidates.append(princ)
        principal = self.to_employee_principal(user_id)
        if principal not in candidates:
            candidates.append(principal)
        groups_removed: list[str] = []
        for gid, members in list(self._groups.items()):
            original = list(members)
            remaining = [m for m in members if m not in candidates and m not in [user_id, principal] and self.to_employee_principal(m) != principal]
            if len(remaining) != len(original):
                groups_removed.append(gid)
                self._groups[gid] = remaining
        for cid in candidates:
            self._user_groups.pop(cid, None)
        removed_users = 0
        for cid in candidates:
            if cid in self._users:
                self._users.pop(cid, None)
                removed_users += 1
        for k in list(self._users.keys()):
            u = self._users[k]
            email_k = u.get("email") or ""
            princ_k = u.get("employee_principal") or self.to_employee_principal(email_k or k)
            if princ_k == principal or email_k == user_id or k in candidates:
                self._users.pop(k, None)
        for k in list(self._principal_map.keys()):
            if self._principal_map[k] == principal or k in candidates:
                del self._principal_map[k]
        revoked: list[str] = []
        if ds is not None:
            try:
                if hasattr(ds, "list_by_user"):
                    delegs = ds.list_by_user(principal) or []
                    if not delegs and hasattr(ds, "_store"):
                        delegs = [d for d in getattr(ds, "_store", {}).values() if getattr(d, "user_id", "") in candidates or getattr(d, "user_id", "") == principal]
                else:
                    delegs = []
                for d in delegs:
                    did = getattr(d, "id", None) or (d.get("id") if isinstance(d, dict) else None)
                    if did:
                        try:
                            # Only revoke if still active (idempotent second call should be no-op)
                            try:
                                if hasattr(ds, "is_active") and not ds.is_active(did):
                                    continue
                                if hasattr(ds, "get"):
                                    cur = ds.get(did)
                                    if cur is not None and getattr(getattr(cur, "status", None), "value", "") == "REVOKED":
                                        continue
                            except Exception:
                                pass
                            ds.revoke(did)
                            revoked.append(did)
                        except Exception:
                            pass
            except Exception:
                pass
        audit_events: list[Any] = []
        if ledger is not None:
            try:
                now = datetime.now(timezone.utc)
                for did in revoked:
                    try:
                        if _has_audit:
                            evt = AuditEvent(  # type: ignore
                                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                                event_type=AuditEventType.DELEGATION_REVOKED,  # type: ignore
                                timestamp=now,
                                tenant_id=self.tenant_id,
                                user_id=principal,
                                agent_id=self.to_agent_principal(principal),
                                resource=f"delegation/{did}",
                                action="REVOKE",
                                decision="REVOKED",
                                delegation_id=did,
                            )
                        else:
                            evt = {"event_id": f"evt_{uuid.uuid4().hex[:12]}", "event_type": "DELEGATION_REVOKED", "timestamp": now.isoformat(), "tenant_id": self.tenant_id, "user_id": principal, "delegation_id": did, "reason": reason}
                        if hasattr(ledger, "append"):
                            ledger.append(evt)
                        audit_events.append(evt)
                    except Exception:
                        pass
                try:
                    if _has_audit:
                        evt2 = AuditEvent(  # type: ignore
                            event_id=f"evt_{uuid.uuid4().hex[:12]}",
                            event_type=AuditEventType.DELEGATION_REVOKED,  # type: ignore
                            timestamp=now,
                            tenant_id=self.tenant_id,
                            user_id=principal,
                            agent_id=self.to_agent_principal(principal),
                            resource=f"user/{principal}",
                            action="DEPROVISION",
                            decision="REVOKED",
                        )
                        if hasattr(ledger, "append"):
                            ledger.append(evt2)
                        audit_events.append(evt2)
                except Exception:
                    pass
            except Exception:
                pass
        return {
            "user_id": user_id,
            "principal": principal,
            "removed_users": removed_users,
            "groups_removed_from": groups_removed,
            "revoked_delegations": revoked,
            "revoked_count": len(revoked),
            "audit_events": len(audit_events),
            "reason": reason,
        }

    async def sync_user_groups(self, user_id: str, groups: list[str]) -> dict[str, Any]:
        self._user_groups[user_id] = set(groups)
        for g in groups:
            if g not in self._groups:
                self._groups[g] = []
            if user_id not in self._groups[g]:
                self._groups[g].append(user_id)
        return {"user_id": user_id, "groups": groups, "security_domain": self.assign_security_domain(user_id)}

    def required_scope(self, tool_name: str) -> str | None:
        return {"iam_get_user": "directory.read", "iam_list_users": "directory.read", "iam_sync_users": "directory.write", "iam_deprovision_user": "directory.write"}.get(tool_name)

    def tool_action(self, tool_name: str) -> str:
        return self.TOOL_ACTION.get(tool_name, "READ")

    async def list_tools(self) -> list[str]:
        return list(self.TOOL_ACTION.keys())

    async def list_resources(self) -> list[str]:
        return ["iam/user/*", "iam/group/*", "iam/tenant/*"]

    def describe_tools(self) -> list[dict[str, Any]]:
        return [{"name": k, "action": v, "resource_pattern": "iam/*"} for k, v in self.TOOL_ACTION.items()]

    async def call_tool(self, tool_name: str, args: dict[str, Any], agent_context: dict[str, Any] | Any) -> dict[str, Any]:
        if tool_name == "iam_get_user":
            return await self.get_user(args.get("user_id") or args.get("email", ""))
        if tool_name == "iam_list_users":
            return await self.list_users(domain=args.get("domain"), max_results=int(args.get("max_results", 100)))
        if tool_name == "iam_get_group":
            return await self.get_group(args.get("group_id", ""))
        if tool_name == "iam_list_groups":
            return await self.list_groups(domain=args.get("domain"))
        if tool_name == "iam_sync_users":
            return await self.sync_users(args.get("users", []))
        if tool_name == "iam_resolve_principal":
            return self.resolve_principal(args.get("email") or args.get("user_id", ""))
        if tool_name == "iam_deprovision_user":
            return await self.deprovision_user(
                args.get("user_id") or args.get("email", ""),
                delegation_service=args.get("_delegation_service") or self.delegation_service,
                audit_ledger=args.get("_audit_ledger") or self.audit_ledger,
                reason=args.get("reason", "iam_deprovision"),
            )
        if tool_name == "iam_jit_sync":
            return await self.jit_sync_user_groups(args.get("user_id", ""))
        if tool_name == "iam_evaluate_policy":
            r = self.evaluate_access(
                principal=args.get("principal", "") or args.get("user_id", ""),
                action=args.get("action", "READ"),
                resource=args.get("resource", ""),
                groups=args.get("groups"),
                extra_bundles=args.get("extra_bundles"),
            )
            return r.__dict__ if hasattr(r, "__dict__") else dict(r) if isinstance(r, dict) else {"decision": str(getattr(r, "decision", ""))}
        raise ValueError(f"unknown tool: {tool_name}")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "provider": self.iam_provider, "provider_impl": self._provider_impl.provider_name, "tools": list(self.TOOL_ACTION.keys()), "resources": ["iam/*"], "domain": self.domain, "tenant_id": self.tenant_id, "default_security_domain": self.default_security_domain, "has_api_key": bool(self.api_key)}

