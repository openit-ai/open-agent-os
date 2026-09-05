"""OAOS-owned Google OAuth flow — Control Plane (Section 9-10 personal delegation).

Separation of concerns (explicit):
- Admin Console ``GET /v1/oauth/config`` reports IdP config *presence/prefs only*.
  It is NOT user authorization. This module is the ONLY user-token path:
  ``POST /v1/google/oauth/authorize`` -> Google consent ->
  ``GET /v1/google/oauth/callback`` -> Vault store + Delegation bind.
- OAOS NEVER reads Hermes token files. Tokens enter only via Google's token
  endpoint (server-side, ``GOOGLE_CLIENT_SECRET`` stays server-side), are
  stored only through the existing encrypted ``CredentialVault``
  (``access::refresh`` bundle, owner-bound), and persisted only as
  ``secret_ref`` + metadata via the existing Delegation/CredentialBinding
  models. Tokens are never returned by any endpoint.

Identity / header contract:
- ``POST /authorize``, ``GET /status``, ``POST /revoke`` use the existing
  control-plane helper ``resolve_caller_user`` (JWT Bearer in production,
  ``X-User-Id`` fallback only where that helper allows it — tests/nonprod).
  ``X-Tenant-Id`` selects the tenant, ``X-Agent-Id`` (optional) must match the
  deterministic agent derived from the verified user, ``X-Session-Id``
  (optional) is ownership-verified against the session store.
- ``GET /callback`` carries NO Authorization header by design (the user
  arrives from Google's redirect). It relies solely on the one-time,
  expiring, owner-bound ``state`` issued by ``/authorize``.
- LIMITATION: ``X-User-Id``/``X-Agent-Id``/``X-Tenant-Id`` headers are only as
  trustworthy as the ingress that sets them (verified Mattermost webhook /
  session identities or a real JWT). Direct Internet clients must present a
  JWT Bearer token; header-only identity is a nonprod/test convenience. The
  callback's security does not depend on headers at all — only on ``state``.

State store: Redis in production (``REDIS_URL``/``OAOS_REDIS_URL``), in-memory
only outside production. Production fails closed (503) when Redis or the
vault is unavailable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/google/oauth", tags=["google-oauth"])
localhost_callback_router = APIRouter(tags=["google-oauth"])

# ---------------------------------------------------------------------------
# Google endpoints + scopes
# ---------------------------------------------------------------------------

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# Least-privilege defaults: readonly Google scopes + OIDC identity scopes so
# the callback can read the profile email (read-only) for binding checks.
DEFAULT_SCOPES: list[str] = sorted(
    {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/tasks.readonly",
        "openid",
        "email",
        "profile",
    }
)


def _scope_allowlist() -> set[str]:
    """Union of live GoogleConnector/adapter scopes + OIDC scopes.

    Reuses the live connector's scope table when importable so this module
    never drifts from the enforcement point.
    """
    allow: set[str] = {"openid", "email", "profile"}
    for modpath, attr in (
        ("execution_gateway.connectors.google", "GOOGLE_SCOPES"),
        ("google.adapter", "GOOGLE_SCOPES"),
    ):
        try:
            mod = __import__(modpath, fromlist=[attr])
            scopes = getattr(mod, attr, None)
            if isinstance(scopes, dict):
                allow.update(str(v) for v in scopes.values())
        except Exception:
            continue
    if not allow:
        allow.update(DEFAULT_SCOPES)
    return allow


def _validate_scopes(scopes: list[str]) -> list[str]:
    allow = _scope_allowlist()
    cleaned: list[str] = []
    for s in scopes:
        s = (s or "").strip()
        if not s:
            continue
        if s not in allow and not s.startswith("https://www.googleapis.com/auth/"):
            raise HTTPException(status_code=400, detail=f"invalid scope: {s}")
        if s not in cleaned:
            cleaned.append(s)
    if not cleaned:
        raise HTTPException(status_code=400, detail="no scopes requested")
    return cleaned


def _canonical_scope_for_comparison(scope: str) -> str:
    """Normalize Google's equivalent OIDC scope aliases for grant checks.

    Google may return both the short OIDC names requested by the consent URL
    (``email``/``profile``) and their userinfo URI aliases.  They represent
    the same least-privilege identity grants; treating the aliases as an
    unexpected expansion incorrectly rejects a successful user consent.
    This normalization is used only for set comparison, while the originally
    returned scope string remains available as provider metadata.
    """
    aliases = {
        "https://www.googleapis.com/auth/userinfo.email": "email",
        "https://www.googleapis.com/auth/userinfo.profile": "profile",
    }
    return aliases.get(scope.strip(), scope.strip())


def _canonicalize_scope_string(scopes: str) -> str:
    """Return a bounded, de-duplicated scope string for persistence.

    Google can return both short OIDC scopes and their equivalent userinfo URI
    aliases. Persisting the raw response can exceed the deployed
    ``scope VARCHAR(256)`` column even though it grants no additional access.
    Keep the first provider order for readable metadata while collapsing only
    the explicitly equivalent aliases.
    """
    result: list[str] = []
    seen: set[str] = set()
    for raw in (scopes or "").split():
        canonical = _canonical_scope_for_comparison(raw)
        if canonical and canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return " ".join(result)


# ---------------------------------------------------------------------------
# Env / production helpers (presence only — values never logged/returned)
# ---------------------------------------------------------------------------


def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        if os.getenv(k, "").strip().lower() in ("production", "prod"):
            return True
    return False


def _state_ttl_seconds() -> int:
    try:
        return max(60, int(os.getenv("OAOS_GOOGLE_OAUTH_STATE_TTL", "600")))
    except ValueError:
        return 600


def _google_client_id() -> str:
    return (os.getenv("GOOGLE_CLIENT_ID", "") or "").strip()


def _google_redirect_uri() -> str:
    return (os.getenv("GOOGLE_REDIRECT_URI", "") or "").strip()


def _google_configured() -> bool:
    # Presence check only; the secret value itself is touched solely inside
    # the token-exchange call below.
    return bool(_google_client_id()) and bool((os.getenv("GOOGLE_CLIENT_SECRET", "") or "").strip()) and bool(
        _google_redirect_uri()
    )


def _redis_url() -> str:
    return (os.getenv("REDIS_URL", "") or os.getenv("OAOS_REDIS_URL", "") or "").strip()


# ---------------------------------------------------------------------------
# OAuth state store — Redis in prod, memory otherwise
# ---------------------------------------------------------------------------


@dataclass
class OAuthStateEntry:
    state: str
    tenant_id: str
    user_id: str  # employee:...
    agent_id: str  # agent:assistant:...
    session_id: str | None = None
    expected_email: str | None = None  # lowercased registered/login-hint email
    scopes: list[str] = field(default_factory=list)
    code_verifier: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "expected_email": self.expected_email,
            "scopes": list(self.scopes),
            "code_verifier": self.code_verifier,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OAuthStateEntry":
        raw_scopes = d.get("scopes") or []
        scopes = [str(s) for s in raw_scopes] if isinstance(raw_scopes, list) else []
        return cls(
            state=str(d.get("state", "")),
            tenant_id=str(d.get("tenant_id", "default")),
            user_id=str(d.get("user_id", "")),
            agent_id=str(d.get("agent_id", "")),
            session_id=d.get("session_id"),
            expected_email=d.get("expected_email"),
            scopes=scopes,
            code_verifier=str(d.get("code_verifier", "")),
            created_at=float(d.get("created_at", 0.0)),
            expires_at=float(d.get("expires_at", 0.0)),
        )


class MemoryOAuthStateStore:
    """Nonprod one-time state store with expiry + replay protection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, OAuthStateEntry] = {}

    def save(self, entry: OAuthStateEntry) -> None:
        with self._lock:
            self._states[entry.state] = entry

    def consume(self, state: str) -> OAuthStateEntry | None:
        """One-time take: second use returns None (replay denied)."""
        with self._lock:
            entry = self._states.pop(state, None)
        if entry is None:
            return None
        if entry.expired():
            return None
        return entry

    def clear(self) -> None:
        with self._lock:
            self._states.clear()


class RedisOAuthStateStore:
    """Production state store — atomic one-time consume via GETDEL."""

    KEY_PREFIX = "oaos:google_oauth:state:"
    _PREFIX_LEN = len(KEY_PREFIX)

    def __init__(self, url: str) -> None:
        try:
            import redis as _redis  # type: ignore
        except ImportError as e:
            raise RuntimeError("redis library unavailable") from e
        self._client = _redis.Redis.from_url(url, decode_responses=True, socket_timeout=1.0)
        try:
            self._client.ping()
        except Exception as e:
            raise RuntimeError(f"OAuth state Redis unavailable: {e}") from e

    def _key(self, state: str) -> str:
        # state tokens are urlsafe randoms; still sanitize for key safety.
        safe = "".join(c for c in state if c.isalnum() or c in "-_")[:128]
        return f"{self.KEY_PREFIX}{safe}"

    def save(self, entry: OAuthStateEntry) -> None:
        import json as _json

        ttl = max(1, int(entry.expires_at - time.time()))
        ok = self._client.set(self._key(entry.state), _json.dumps(entry.to_dict()), nx=True, ex=ttl)
        if not ok:
            raise RuntimeError("OAuth state collision — retry authorize")

    def consume(self, state: str) -> OAuthStateEntry | None:
        import json as _json

        raw: Any = None
        try:
            raw = self._client.getdel(self._key(state))  # type: ignore[attr-defined]
        except AttributeError:
            raw = self._client.get(self._key(state))
            if raw is not None:
                self._client.delete(self._key(state))
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not raw:
            return None  # unknown or already consumed (replay)
        try:
            entry = OAuthStateEntry.from_dict(_json.loads(raw))
        except Exception:
            return None
        if entry.expired():
            return None
        return entry


_memory_state_store = MemoryOAuthStateStore()
_state_store_override: Any | None = None


def set_state_store_override(store: Any | None) -> None:
    global _state_store_override
    _state_store_override = store


def get_state_store() -> Any:
    """Redis in production, memory otherwise. Prod fails closed."""
    if _state_store_override is not None:
        return _state_store_override
    if _is_production():
        url = _redis_url()
        if not url or "://" not in url:
            raise HTTPException(status_code=503, detail="OAuth state store unavailable (REDIS_URL required in production)")
        try:
            return RedisOAuthStateStore(url)
        except RuntimeError as e:
            logger.error("OAuth state store unavailable: %s", type(e).__name__)
            raise HTTPException(status_code=503, detail="OAuth state store unavailable") from e
    return _memory_state_store


# ---------------------------------------------------------------------------
# Vault + DelegationService singletons (lazy; injected in tests)
# ---------------------------------------------------------------------------


def _prepare_security_import_paths() -> None:
    """Make the repository's hyphenated security package roots importable.

    The production units set the repository root on ``PYTHONPATH`` but the
    Vault and delegation packages live below directories named
    ``credential-vault`` and ``delegation``.  Do this from the module location
    rather than depending on a unit-file typo or a caller's current directory.
    """
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    for path in (
        repo_root / "security",
        repo_root / "security" / "credential-vault",
        repo_root / "security" / "delegation",
        repo_root / "packages" / "delegation-model",
    ):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)


_vault_override: Any | None = None
_vault_singleton: Any | None = None
_delegation_override: Any | None = None
_delegation_singleton: Any | None = None


def set_vault_override(vault: Any | None) -> None:
    global _vault_override
    _vault_override = vault


def set_delegation_service_override(svc: Any | None) -> None:
    global _delegation_override
    _delegation_override = svc


def reset_oauth_overrides() -> None:
    global _vault_override, _delegation_override, _state_store_override
    global _vault_singleton, _delegation_singleton
    _vault_override = None
    _delegation_override = None
    _state_store_override = None
    _vault_singleton = None
    _delegation_singleton = None
    _memory_state_store.clear()
    _bindings.clear()


def get_vault() -> Any | None:
    """Existing encrypted CredentialVault. None when unavailable.

    Import paths are prepared lazily because this module is also used by
    isolated tests that load the Control Plane package without installation.
    Production callers fail closed when the vault is unavailable.

    The vault object is created only after the repository security package
    roots are made importable; no token bytes are kept in this module.
    """
    _prepare_security_import_paths()
    if _vault_override is not None:
        return _vault_override
    global _vault_singleton
    if _vault_singleton is not None:
        return _vault_singleton
    # Preferred: existing factory (owns the encryption-key wiring).
    for modpath, factory in (("vault", "create_vault"), ("security.credential-vault.vault", "create_vault")):
        try:
            mod = __import__(modpath, fromlist=[factory])
            fn = getattr(mod, factory, None)
            if callable(fn):
                db_url = os.getenv("OAOS_DATABASE_URL") or os.getenv("DATABASE_URL") or None
                delegation_service = get_delegation_service()
                _vault_singleton = fn(db_url=db_url, delegation_service=delegation_service)
                return _vault_singleton
        except Exception as exc:
            logger.warning("Google OAuth vault factory unavailable: %s", type(exc).__name__)
            continue
    # Fallback: direct class (nonprod only; prod fails closed below).
    for modpath in ("vault.vault", "security.credential-vault.vault.vault"):
        try:
            mod = __import__(modpath, fromlist=["EncryptedPostgresVault"])
            cls = getattr(mod, "EncryptedPostgresVault", None)
            if cls is not None and not _is_production():
                import warnings

                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    _vault_singleton = cls(encryption_key=b"oaos-control-plane-google-oauth-dev")
                return _vault_singleton
        except Exception:
            continue
    return None


def require_vault() -> Any:
    vault = get_vault()
    if vault is None:
        raise HTTPException(status_code=503, detail="credential vault unavailable; fail-closed")
    if _is_production():
        # A process-local vault would make credentials disappear across the
        # Control Plane/Execution Gateway boundary. Production requires a
        # durable DB session maker (or a fully configured external backend).
        if getattr(vault, "_session_maker", None) is None:
            raise HTTPException(status_code=503, detail="durable credential vault unavailable; fail-closed")
    return vault


def get_delegation_service() -> Any | None:
    if _delegation_override is not None:
        return _delegation_override
    global _delegation_singleton
    if _delegation_singleton is not None:
        return _delegation_singleton
    _prepare_security_import_paths()
    for modpath in ("delegation_service.service", "delegation.service", "security.delegation.delegation_service.service"):
        try:
            mod = __import__(modpath, fromlist=["DelegationService"])
            cls = getattr(mod, "DelegationService", None)
            if cls is not None:
                _delegation_singleton = cls()
                return _delegation_singleton
        except Exception as exc:
            logger.warning("Google OAuth delegation service unavailable: %s", type(exc).__name__)
            continue
    return None


# ---------------------------------------------------------------------------
# Binding metadata index (secret_ref mapping; delegation rows stay canonical)
# ---------------------------------------------------------------------------
# Delegation/CredentialBinding rows (via DelegationService, DB-backed when
# configured) are the canonical persistence. This index maps delegation_id ->
# {secret_ref + non-secret metadata} so status/revoke can resolve the vault
# ref; entries hold NO token material.

_bindings: dict[str, dict[str, Any]] = {}
_bindings_lock = threading.Lock()


def _binding_save(delegation_id: str, meta: dict[str, Any]) -> None:
    with _bindings_lock:
        _bindings[delegation_id] = meta


def _binding_get(delegation_id: str) -> dict[str, Any] | None:
    with _bindings_lock:
        return _bindings.get(delegation_id)


def _binding_pop(delegation_id: str) -> dict[str, Any] | None:
    with _bindings_lock:
        return _bindings.pop(delegation_id, None)


def _resolve_secret_ref(svc: Any | None, delegation_id: str) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve an active Google binding without trusting the process cache.

    The in-memory index is only a fast-path for metadata. When a delegation
    service is available, its active binding is authoritative; a cached ref is
    not accepted if the service says the delegation/binding is revoked.
    """
    cached = _binding_get(delegation_id)
    candidates: list[Any] = []
    if svc is not None:
        try:
            list_fn = getattr(svc, "list_bindings_for_delegation", None)
            if callable(list_fn):
                listed = list_fn(delegation_id)
                candidates = list(listed) if isinstance(listed, (list, tuple, set)) else []
        except Exception:
            candidates = []
        if not candidates:
            store = getattr(svc, "_bindings", None)
            if isinstance(store, dict):
                candidates = [b for b in store.values() if getattr(b, "delegation_id", None) == delegation_id]
        active: list[Any] = []
        for binding in candidates:
            status = getattr(getattr(binding, "status", None), "value", getattr(binding, "status", None))
            if str(status).upper() != "ACTIVE" or getattr(binding, "provider", "google") != "google":
                continue
            binding_id = getattr(binding, "id", None)
            if binding_id and hasattr(svc, "is_binding_active"):
                try:
                    if not svc.is_binding_active(binding_id):
                        continue
                except Exception:
                    continue
            ref = getattr(binding, "secret_ref", None)
            if ref:
                return str(ref), cached
            active.append(binding)
        # A service-backed delegation with no active binding must not fall
        # through to a stale in-memory reference.
        if candidates:
            return None, cached
    if cached and cached.get("secret_ref"):
        return str(cached["secret_ref"]), cached
    return None, cached


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def _new_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


# ---------------------------------------------------------------------------
# Identity helpers — reuse existing control-plane auth patterns
# ---------------------------------------------------------------------------


def _normalize_user(value: str) -> str:
    raw = (value or "").strip()
    return raw if raw.startswith("employee:") else f"employee:{raw}" if raw else ""


def _resolve_owner(
    authorization: str | None,
    x_user_id: str | None,
    x_tenant_id: str | None,
    x_agent_id: str | None,
    session_id: str | None = None,
) -> tuple[str, str, str]:
    """Return (tenant_id, user_id, agent_id) for a verified caller.

    Uses ``resolve_caller_user`` (JWT in prod). The optional agent/session
    bindings are verified, never trusted blindly.
    """
    try:
        from .auth import resolve_caller_user, verify_user_jwt  # type: ignore
    except ImportError:  # pragma: no cover
        from control_plane.auth import resolve_caller_user, verify_user_jwt  # type: ignore

    caller = resolve_caller_user(authorization, x_user_id)
    caller = _normalize_user(caller)
    tenant = (x_tenant_id or "default").strip() or "default"

    # When a real JWT is presented, its tenant must match the header tenant.
    if authorization and authorization.strip().lower().startswith("bearer "):
        token = authorization.strip()[7:].strip()
        if token:
            claims = verify_user_jwt(token)
            token_tenant = str(claims.get("tenant_id") or "")
            if token_tenant and token_tenant != tenant:
                raise HTTPException(status_code=401, detail="TENANT_MISMATCH: token tenant != X-Tenant-Id")

    # Agent binding: derive deterministically; an explicit header must match.
    try:
        try:
            from .identity import map_user_to_agent  # type: ignore
        except ImportError:  # pragma: no cover
            from control_plane.identity import map_user_to_agent  # type: ignore
        mapping = map_user_to_agent(caller, tenant)
        agent_id = mapping.agent_principal
    except HTTPException:
        raise
    except Exception:
        suffix = caller.split(":", 1)[-1] if ":" in caller else caller
        agent_id = f"agent:assistant:{suffix}"
    if x_agent_id and x_agent_id.strip() and x_agent_id.strip() != agent_id:
        raise HTTPException(status_code=403, detail="agent mismatch: X-Agent-Id != derived agent")

    # Session binding: ownership-verified via the session store when given.
    if session_id:
        try:
            try:
                from .session import session_store  # type: ignore
            except ImportError:  # pragma: no cover
                from control_plane.session import session_store  # type: ignore
            rec = session_store.get(session_id, caller)
            if rec.tenant_id != tenant:
                raise HTTPException(status_code=403, detail="tenant mismatch: session tenant != X-Tenant-Id")
            if rec.agent_id != agent_id:
                raise HTTPException(status_code=403, detail="agent mismatch: session agent != derived agent")
        except HTTPException:
            raise
        except (KeyError, PermissionError) as e:
            raise HTTPException(status_code=403 if isinstance(e, PermissionError) else 404, detail=str(e))

    return tenant, caller, agent_id


def _registered_email(tenant_id: str, user_id: str) -> str | None:
    """Opportunistic registered-email lookup (None when unavailable).

    The current ``admin_user_mappings`` schema has no dedicated email column,
    so this checks the mapping payload for any email-shaped field
    (``email``/``google_email``/``registered_email``/``extra.email``) without
    ever inferring an owner. No mapping/DB => None => the callback skips the
    email cross-check (recorded as ``userinfo_verified=false``) instead of
    blocking; the authorize call may bind an explicit expected email.
    """
    try:
        try:
            from .user_mapping_lookup import lookup_registered_owner  # type: ignore
        except ImportError:  # pragma: no cover
            from control_plane.user_mapping_lookup import lookup_registered_owner  # type: ignore
        mapping = lookup_registered_owner(tenant_id, user_id)
    except Exception:
        return None
    if not mapping:
        return None
    for key in ("email", "google_email", "registered_email", "googleEmail"):
        val = (mapping.get(key) or "").strip() if isinstance(mapping, dict) else ""
        if val and "@" in val:
            return val.lower()
    try:
        extra = mapping.get("extra") if isinstance(mapping, dict) else None
        if isinstance(extra, dict):
            for key in ("email", "google_email", "registered_email"):
                val = str(extra.get(key) or "").strip()
                if val and "@" in val:
                    return val.lower()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# HTTP client factory (injectable for tests; httpx in production)
# ---------------------------------------------------------------------------

_http_client_factory: Callable[[], Any] | None = None


def set_http_client_factory(factory: Callable[[], Any] | None) -> None:
    global _http_client_factory
    _http_client_factory = factory


def _http_client() -> Any:
    if _http_client_factory is not None:
        return _http_client_factory()
    try:
        import httpx  # type: ignore
    except ImportError as e:
        raise HTTPException(status_code=503, detail="HTTP client unavailable (httpx required)") from e
    return httpx.AsyncClient(timeout=15)


async def _exchange_code_for_tokens(code: str, code_verifier: str) -> dict[str, Any]:
    """Server-side token exchange. Client secret never leaves this call."""
    data = {
        "code": code,
        "client_id": _google_client_id(),
        "client_secret": (os.getenv("GOOGLE_CLIENT_SECRET", "") or "").strip(),
        "redirect_uri": _google_redirect_uri(),
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    async with _http_client() as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data=data)
        resp.raise_for_status()
        tok = resp.json()
    if not isinstance(tok, dict) or not tok.get("access_token"):
        raise HTTPException(status_code=502, detail="token exchange failed: no access_token")
    return tok


async def _fetch_profile_email(access_token: str) -> tuple[str | None, bool]:
    """Read-only userinfo fetch. Returns (email_lower_or_None, verified)."""
    try:
        async with _http_client() as client:
            resp = await client.get(
                GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            resp.raise_for_status()
            info = resp.json()
    except Exception as e:
        logger.warning("google userinfo fetch failed: %s", type(e).__name__)
        return None, False
    if not isinstance(info, dict):
        return None, False
    email = str(info.get("email") or "").strip().lower() or None
    verified = bool(info.get("email_verified", False))
    return email, verified


# ---------------------------------------------------------------------------
# Request/response models (metadata only — never tokens)
# ---------------------------------------------------------------------------


class AuthorizeRequest(BaseModel):
    session_id: str | None = None
    scopes: list[str] | None = None
    # Optional login hint: also bound as the expected profile email and
    # verified at callback (mismatch => deny + no storage).
    expected_email: str | None = None


class RevokeRequest(BaseModel):
    delegation_id: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/authorize")
async def authorize(
    req: AuthorizeRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> dict[str, Any]:
    """Issue a Google consent URL + one-time owner-bound state."""
    tenant_id, user_id, agent_id = _resolve_owner(authorization, x_user_id, x_tenant_id, x_agent_id, req.session_id)
    if not _google_configured():
        # Fail closed in production; nonprod gets a clear misconfiguration.
        raise HTTPException(
            status_code=503 if _is_production() else 500,
            detail="Google OAuth not configured (GOOGLE_CLIENT_ID/GOOGLE_REDIRECT_URI required)",
        )
    require_vault()  # fail closed in prod before issuing state for an unusable flow

    scopes = _validate_scopes(req.scopes or DEFAULT_SCOPES)

    expected_email = (req.expected_email or "").strip().lower() or None
    if expected_email and "@" not in expected_email:
        raise HTTPException(status_code=400, detail="invalid expected_email")
    registered = _registered_email(tenant_id, user_id)
    if registered:
        if expected_email and expected_email != registered:
            raise HTTPException(status_code=403, detail="expected_email does not match registered mapping email")
        expected_email = registered

    state = secrets.token_urlsafe(32)
    verifier = _new_code_verifier()
    ttl = _state_ttl_seconds()
    entry = OAuthStateEntry(
        state=state,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=req.session_id,
        expected_email=expected_email,
        scopes=scopes,
        code_verifier=verifier,
        expires_at=time.time() + ttl,
    )
    get_state_store().save(entry)

    params = {
        "client_id": _google_client_id(),
        "redirect_uri": _google_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": _code_challenge(verifier),
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    }
    if expected_email:
        params["login_hint"] = expected_email
    auth_url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    logger.info("google oauth authorize tenant=%s user=%s scopes=%d", tenant_id, user_id, len(scopes))
    return {"authorization_url": auth_url, "state": state, "expires_in": ttl, "scopes": scopes}


@router.get("/callback")
async def callback(
    state: str = Query(default=""),
    code: str = Query(default=""),
    error: str | None = Query(default=None),
) -> dict[str, Any]:
    """Consume ``state`` + ``code``: exchange, verify, vault-store, bind.

    Auth relies on the one-time state, not on request headers.
    Returns metadata only; never returns token material.
    """
    if error:
        raise HTTPException(status_code=400, detail=f"Google authorization failed: {error}")
    if not state or not code:
        raise HTTPException(status_code=400, detail="state and code are required")

    entry = get_state_store().consume(state)
    if entry is None:
        raise HTTPException(status_code=400, detail="invalid, expired, or already-used state")

    tok = await _exchange_code_for_tokens(code, entry.code_verifier)
    scope = _canonicalize_scope_string(
        str(tok.get("scope") or " ".join(entry.scopes))
    )
    expires_in = int(tok.get("expires_in", 3600) or 3600)

    # Read-only profile check against the bound expected email (if any).
    access_token = str(tok.get("access_token"))
    profile_email, email_verified = await _fetch_profile_email(access_token)
    if entry.expected_email:
        if not profile_email:
            raise HTTPException(status_code=502, detail="profile email unavailable — cannot verify binding; no credentials stored")
        if profile_email != entry.expected_email:
            logger.warning("google oauth profile email mismatch for user=%s", entry.user_id)
            raise HTTPException(status_code=403, detail="PROFILE_EMAIL_MISMATCH: Google account does not match registered email; no credentials stored")
    userinfo_verified = bool(profile_email and email_verified and (not entry.expected_email or profile_email == entry.expected_email))

    # Reject token responses that grant scopes outside the requested set.
    # Google may return a narrower set, but an unexpected broader grant must
    # never silently widen the delegation recorded by OAOS.
    granted_scopes = {
        _canonical_scope_for_comparison(s) for s in scope.split() if s
    }
    requested_scopes = {
        _canonical_scope_for_comparison(s) for s in entry.scopes if s
    }
    if not granted_scopes.issubset(requested_scopes):
        raise HTTPException(status_code=403, detail="Google returned scopes outside the requested allowlist; no credentials stored")

    # Owner-bound encrypted storage. Bundle shape matches the existing
    # GoogleAdapter convention (access::refresh) for connector interop.
    vault = require_vault()
    refresh_token = tok.get("refresh_token") or ""
    bundle = json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + max(1, expires_in),
        "scope": scope,
    }, separators=(",", ":")).encode()
    try:
        secret_ref = await vault.store(entry.user_id, "google", scope, bundle)
    except Exception as e:
        logger.error("credential vault store failed: %s", type(e).__name__)
        raise HTTPException(status_code=503, detail="credential vault store failed") from e

    # Persist delegation + credential binding via existing models/services.
    delegation_id = f"dlg_{uuid.uuid4().hex[:12]}"
    binding_id: str | None = None
    svc = get_delegation_service()
    if svc is not None:
        try:
            delegation = svc.grant(entry.user_id, entry.agent_id, "google", scope)
            delegation_id = getattr(delegation, "id", delegation_id)
            binding = svc.bind_credential(delegation_id, "google", secret_ref, scope)
            binding_id = getattr(binding, "id", None)
        except Exception as e:
            # Vault already holds the secret: revoke it to avoid orphans, then fail.
            try:
                await vault.revoke(secret_ref)
            except Exception:
                pass
            logger.error("delegation persist failed: %s", type(e).__name__)
            raise HTTPException(status_code=503, detail="delegation persist failed") from e
    elif _is_production():
        try:
            await vault.revoke(secret_ref)
        except Exception:
            pass
        raise HTTPException(status_code=503, detail="delegation service unavailable; fail-closed")

    _binding_save(
        delegation_id,
        {
            "secret_ref": secret_ref,
            "binding_id": binding_id,
            "tenant_id": entry.tenant_id,
            "user_id": entry.user_id,
            "agent_id": entry.agent_id,
            "session_id": entry.session_id,
            "provider": "google",
            "scope": scope,
            "google_email": profile_email,
            "userinfo_verified": userinfo_verified,
            "has_refresh_token": bool(refresh_token),
            "expires_in": expires_in,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    logger.info("google oauth exchange user=%s delegation=%s scope=%s", entry.user_id, delegation_id, scope)
    return {
        "delegation_id": delegation_id,
        "provider": "google",
        "scope": scope,
        "expires_in": expires_in,
        "has_refresh_token": bool(refresh_token),
        "google_email": profile_email,
        "userinfo_verified": userinfo_verified,
        "status": "ACTIVE",
    }


@localhost_callback_router.get("/callback")
async def localhost_callback(
    state: str = Query(default=""),
    code: str = Query(default=""),
    error: str | None = Query(default=None),
) -> dict[str, Any]:
    """SSH-tunnel-only localhost redirect alias.

    Google is registered with ``http://localhost:49697/callback``. The
    operator's browser forwards that local port through an SSH tunnel to the
    Control Plane, where the same one-time state/callback logic is applied.
    """
    return await callback(state=state, code=code, error=error)


def _require_binding_owner(delegation_id: str, tenant_id: str, user_id: str) -> tuple[Any | None, str, dict[str, Any] | None]:
    svc = get_delegation_service()
    delegation = None
    if svc is not None:
        try:
            delegation = svc.get(delegation_id)
        except Exception:
            delegation = None
    secret_ref, meta = _resolve_secret_ref(svc, delegation_id)
    owner_user = str(getattr(delegation, "user_id", "") or (meta or {}).get("user_id") or "")
    if delegation is None and meta is None:
        raise HTTPException(status_code=404, detail="delegation not found")
    if owner_user and owner_user != user_id:
        raise HTTPException(status_code=403, detail="cross-user access denied")
    if meta and meta.get("tenant_id") and meta.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=403, detail="tenant mismatch")
    # A revoked grant remains visible as metadata, but has no usable secret.
    # This preserves status/read-back while keeping execution fail-closed.
    if delegation is not None:
        raw_status = getattr(delegation, "status", None)
        status_value = str(getattr(raw_status, "value", raw_status) or "").upper()
        if status_value == "REVOKED":
            if secret_ref is None:
                secret_ref = str((meta or {}).get("secret_ref") or "") or None
            return delegation, str(secret_ref or ""), meta
    if meta and str(meta.get("status", "")).upper() == "REVOKED":
        if secret_ref is None:
            secret_ref = str(meta.get("secret_ref") or "") or None
        return delegation, str(secret_ref or ""), meta
    if secret_ref is None:
        raise HTTPException(status_code=404, detail="credential binding not found")
    return delegation, secret_ref, meta


@router.get("/status")
async def status(
    delegation_id: str = Query(default=""),
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> dict[str, Any]:
    """Metadata for a delegation. Owner-only. Never returns tokens."""
    if not delegation_id:
        raise HTTPException(status_code=400, detail="delegation_id is required")
    tenant_id, user_id, _agent_id = _resolve_owner(authorization, x_user_id, x_tenant_id, x_agent_id)
    delegation, secret_ref, meta = _require_binding_owner(delegation_id, tenant_id, user_id)
    svc = get_delegation_service()
    active = bool(svc.is_active(delegation_id)) if svc is not None and delegation is not None else meta is not None
    _dstatus_raw = getattr(delegation, "status", None)
    _dstatus_val = getattr(_dstatus_raw, "value", _dstatus_raw)
    dstatus = str(_dstatus_val) if _dstatus_val is not None else ("ACTIVE" if active else "REVOKED")
    if meta and str(meta.get("status", "")).upper() == "REVOKED":
        dstatus = "REVOKED"
    scope = str(getattr(delegation, "scope", "") or (meta or {}).get("scope") or "")
    return {
        "delegation_id": delegation_id,
        "provider": "google",
        "status": dstatus,
        "scope": scope,
        "secret_ref": secret_ref,
        "google_email": (meta or {}).get("google_email"),
        "userinfo_verified": bool((meta or {}).get("userinfo_verified", False)),
        "has_refresh_token": bool((meta or {}).get("has_refresh_token", False)),
    }


@router.post("/revoke")
async def revoke(
    req: RevokeRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> dict[str, Any]:
    """Revoke Google grant + vault secret + delegation. Metadata only."""
    tenant_id, user_id, agent_id = _resolve_owner(authorization, x_user_id, x_tenant_id, x_agent_id)
    delegation, secret_ref, meta = _require_binding_owner(req.delegation_id, tenant_id, user_id)
    assert secret_ref is not None

    vault = require_vault()
    # Best-effort Google-side revocation: retrieve the bundle owner-bound,
    # revoke refresh (else access) at Google, never expose either.
    google_revoked = False
    try:
        raw = await vault.retrieve(secret_ref, agent_id)
        parts = raw.decode(errors="replace").split("::", 1)
        token_for_revoke = (parts[1] if len(parts) > 1 and parts[1] else parts[0]).strip()
        if token_for_revoke:
            try:
                async with _http_client() as client:
                    resp = await client.post(GOOGLE_REVOKE_URL, data={"token": token_for_revoke})
                    google_revoked = getattr(resp, "status_code", 500) == 200
            except Exception:
                google_revoked = False
    except (PermissionError, KeyError):
        raise
    except Exception as e:
        logger.error("credential vault retrieve failed: %s", type(e).__name__)
        raise HTTPException(status_code=503, detail="credential vault retrieve failed") from e

    try:
        await vault.revoke(secret_ref)
    except Exception as e:
        logger.error("credential vault revoke failed: %s", type(e).__name__)
        raise HTTPException(status_code=503, detail="credential vault revoke failed") from e

    svc = get_delegation_service()
    if svc is not None:
        try:
            svc.revoke(req.delegation_id)
        except Exception:
            pass
    # Retain non-secret revoked metadata for owner-scoped status/read-back;
    # remove only the live secret reference so subsequent execution cannot use it.
    revoked_meta = _binding_get(req.delegation_id) or meta or {}
    revoked_meta = {**revoked_meta, "status": "REVOKED", "secret_ref": secret_ref}
    _binding_save(req.delegation_id, revoked_meta)
    logger.info("google oauth revoke user=%s delegation=%s google_ok=%s", user_id, req.delegation_id, google_revoked)
    return {"delegation_id": req.delegation_id, "status": "REVOKED", "google_revoked": google_revoked}


__all__ = [
    "router",
    "localhost_callback_router",
    "get_state_store",
    "set_state_store_override",
    "get_vault",
    "set_vault_override",
    "get_delegation_service",
    "set_delegation_service_override",
    "set_http_client_factory",
    "reset_oauth_overrides",
    "MemoryOAuthStateStore",
    "RedisOAuthStateStore",
    "OAuthStateEntry",
    "DEFAULT_SCOPES",
]
