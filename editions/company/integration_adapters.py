"""Optional integration adapters for the Company edition (G11/G12, C11-C14).

An organization activates only the services it already uses. The database
(`007_integration_adapters.sql`) is the policy source of truth; this module
applies the same contract in the request path so a provider that is SKIP (or
whose organization app is missing) is never called, and so a personal
delegation never crosses the owner boundary.

No credential material is stored or echoed here. Callers pass the organization
app secret or a resolved vault envelope from the Company EnvironmentFile.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Iterable, Mapping

# Organization app settings live in the EnvironmentFile; the database stores
# only non-secret metadata. These read-only scope allowlists mirror the SQL
# function company.integration_scope_allowed.
PRODUCTIVITY_SCOPES = {
    "google_workspace": frozenset({
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/documents.readonly",
    }),
    "microsoft_365": frozenset({
        "Mail.Read", "Files.Read", "Calendars.Read", "User.Read",
        "offline_access", "openid", "profile", "email",
    }),
}

SUITE_GROUP = "productivity"
COLLABORATION_GROUP = "collaboration"

SLACK_MAX_SKEW_SECONDS = 300


class IntegrationError(Exception):
    """Base integration error."""


class AdapterDenied(IntegrationError):
    """A provider cannot be used; the reason code carries no secret."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Provider:
    id: str
    group: str
    org_app: bool
    personal_delegation: bool


PROVIDERS: Mapping[str, Provider] = {
    "slack": Provider("slack", COLLABORATION_GROUP, True, False),
    "notion": Provider("notion", COLLABORATION_GROUP, True, False),
    "google_workspace": Provider("google_workspace", SUITE_GROUP, True, True),
    "microsoft_365": Provider("microsoft_365", SUITE_GROUP, True, True),
}


def read_only_scopes(provider: str) -> frozenset[str]:
    return PRODUCTIVITY_SCOPES.get(provider, frozenset())


def scopes_allowed(provider: str, scopes: Iterable[str]) -> bool:
    allowed = read_only_scopes(provider)
    requested = list(scopes)
    return bool(requested) and all(scope in allowed for scope in requested)


class IntegrationRegistry:
    """Resolve the organization selection and gate adapter dispatch.

    ``states`` maps provider -> "selected" | "skip" (default "skip").
    ``apps`` is the set of providers with an active organization app.
    """

    def __init__(self, states: Mapping[str, str] | None = None,
                 apps: Iterable[str] = ()):
        self._states = dict(states or {})
        self._apps = set(apps)

    def known(self, provider: str) -> bool:
        return provider in PROVIDERS

    def selection(self, provider: str) -> str:
        if not self.known(provider):
            raise AdapterDenied("unknown_provider")
        return self._states.get(provider, "skip")

    def require_selected(self, provider: str) -> None:
        if not self.known(provider):
            raise AdapterDenied("unknown_provider")
        if self.selection(provider) != "selected":
            raise AdapterDenied("not_selected")

    def dispatch(self, provider: str, handler, *args, **kwargs):
        """Run ``handler`` for a selected provider with an active app.

        A skipped or unconfigured provider is denied instead of being called,
        so no webhook, sync or API request is issued for a service the
        organization does not use.
        """
        self.require_selected(provider)
        if PROVIDERS[provider].org_app and provider not in self._apps:
            raise AdapterDenied("app_missing")
        return handler(*args, **kwargs)


@dataclass(frozen=True)
class SlackEvent:
    team_id: str
    event_id: str
    user_id: str
    channel_id: str
    text: str


@dataclass(frozen=True)
class RoutedEvent:
    event: SlackEvent
    member_id: str
    assistant_id: str


class SlackAdapter:
    """Signature-verified Slack events routed to the sender's own assistant."""

    name = "slack"

    def __init__(self, registry: IntegrationRegistry,
                 resolve_sender, lookup_assistant):
        self._registry = registry
        self._resolve_sender = resolve_sender
        self._lookup_assistant = lookup_assistant

    @staticmethod
    def signature(signing_secret: str, timestamp: str, body: bytes) -> str:
        base = b"v0:" + timestamp.encode("ascii") + b":" + body
        return "v0=" + hmac.new(signing_secret.encode("utf-8"), base,
                                hashlib.sha256).hexdigest()

    def verify_event(self, signing_secret: str, timestamp: str,
                     signature: str | None, body: bytes, now: int,
                     seen_event_ids: set[str], event: SlackEvent) -> SlackEvent:
        if not signature:
            raise AdapterDenied("signature_missing")
        try:
            skew = abs(now - int(timestamp))
        except (TypeError, ValueError):
            raise AdapterDenied("stale_timestamp") from None
        if skew > SLACK_MAX_SKEW_SECONDS:
            raise AdapterDenied("stale_timestamp")
        expected = self.signature(signing_secret, timestamp, body)
        if not hmac.compare_digest(expected, signature):
            raise AdapterDenied("signature_mismatch")
        if event.event_id in seen_event_ids:
            raise AdapterDenied("duplicate_event")
        seen_event_ids.add(event.event_id)
        return event

    def route(self, event: SlackEvent) -> RoutedEvent:
        self._registry.require_selected(self.name)
        account = self._resolve_sender(event.team_id, event.user_id)
        if account is None:
            raise AdapterDenied("unregistered")
        member_id = account.get("member_id")
        if not account.get("active") or not member_id:
            raise AdapterDenied("inactive")
        assistant_id = self._lookup_assistant(member_id)
        if not assistant_id:
            raise AdapterDenied("no_assistant")
        return RoutedEvent(event, member_id, assistant_id)


@dataclass(frozen=True)
class NotionCandidate:
    page_id: str
    acl_version: str
    body: str


class NotionAdapter:
    """Only explicitly shared pages with a confirmed ACL version are candidates."""

    name = "notion"

    def __init__(self, registry: IntegrationRegistry):
        self._registry = registry

    def candidate(self, shared_page_ids: Iterable[str], page_id: str,
                  acl_version: str, body: str) -> NotionCandidate:
        self._registry.require_selected(self.name)
        # Sharing the integration with a workspace does not grant a member read:
        # the page must be explicitly shared and carry a confirmed ACL version.
        if page_id not in set(shared_page_ids):
            raise AdapterDenied("not_shared")
        if not acl_version:
            raise AdapterDenied("acl_unconfirmed")
        return NotionCandidate(page_id, acl_version, body)

    def readable(self, candidate: NotionCandidate,
                 current_acl_version: str | None) -> bool:
        if current_acl_version is None:
            return False
        return candidate.acl_version == current_acl_version


@dataclass(frozen=True)
class Delegation:
    owner_id: str
    provider: str
    subject_id: str
    scopes: tuple[str, ...]
    active: bool = True
    extra: Mapping[str, str] = field(default_factory=dict)


class SuiteAdapter:
    """Per-member delegated OAuth for Google Workspace / Microsoft 365."""

    def __init__(self, registry: IntegrationRegistry, provider: str):
        if not PROVIDERS.get(provider) or not PROVIDERS[provider].personal_delegation:
            raise ValueError("provider does not support personal delegation")
        self.name = provider
        self._registry = registry

    def authorize(self, owner_id: str, delegation: Delegation,
                  granted_scopes: Iterable[str]) -> Delegation:
        self._registry.require_selected(self.name)
        # The delegation must belong to the authenticated owner; an
        # administrator cannot silently read another member's mailbox.
        if delegation.owner_id != owner_id:
            raise AdapterDenied("owner_mismatch")
        if delegation.provider != self.name:
            raise AdapterDenied("provider_mismatch")
        if not delegation.active:
            raise AdapterDenied("delegation_unavailable")
        if not scopes_allowed(self.name, granted_scopes):
            raise AdapterDenied("invalid_scope")
        if tuple(granted_scopes) != delegation.scopes:
            raise AdapterDenied("scope_changed")
        return delegation
