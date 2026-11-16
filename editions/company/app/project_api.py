"""Minimal Project API client for the G10 administrator check and DM delivery.

The Company host already trusts the Project installation, but it must not take a
browser-supplied identity at face value. This client verifies the Project user ID
exists, checks the configured administrator role, and sends the one-time code to
that account's Project DM. No token or code is printed or returned to the browser.
"""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


class ProjectAPIError(Exception):
    """The Project API is unavailable or returned an unusable response."""


class ProjectUserUnknown(ProjectAPIError):
    """The supplied Project account does not exist."""


class ProjectUserNotAdmin(ProjectAPIError):
    """The supplied Project account lacks the configured administrator role."""


def _validate_base_url(value):
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc:
        return value.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost"):
        return value.rstrip("/")
    raise ProjectAPIError("Project API URL must be HTTPS or loopback HTTP")


class ProjectAPI:
    def __init__(self, base_url, token, bot_user_id, admin_roles=None, timeout=8):
        self.base_url = _validate_base_url(base_url)
        if not token or any(char in token for char in "\r\n"):
            raise ProjectAPIError("Project API token is missing or invalid")
        if not bot_user_id or any(char in bot_user_id for char in "\r\n /"):
            raise ProjectAPIError("Project bot user ID is missing or invalid")
        self.token = token
        self.bot_user_id = bot_user_id
        self.admin_roles = set((admin_roles or "system_admin").split())
        self.timeout = timeout

    @classmethod
    def from_environment(cls):
        base_url = os.environ.get("OAOS_COMPANY_PROJECT_API_URL", "")
        token = os.environ.get("OAOS_COMPANY_PROJECT_API_TOKEN", "")
        bot_user_id = os.environ.get("OAOS_COMPANY_PROJECT_BOT_USER_ID", "")
        if not base_url or not token or not bot_user_id:
            raise ProjectAPIError("Project API is not configured")
        return cls(base_url, token, bot_user_id,
                   os.environ.get("OAOS_COMPANY_PROJECT_ADMIN_ROLES", "system_admin"))

    def _request(self, method, path, payload=None):
        url = self.base_url + path
        body = None
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json",
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024)
        except HTTPError as exc:
            if exc.code == 404:
                raise ProjectUserUnknown("Project account not found") from exc
            raise ProjectAPIError("Project API request failed") from exc
        except (URLError, OSError) as exc:
            raise ProjectAPIError("Project API is unavailable") from exc
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProjectAPIError("Project API returned an invalid response") from exc

    def get_user(self, project_user_id):
        if not project_user_id or any(char in project_user_id for char in "\r\n/"):
            raise ProjectUserUnknown("Project account not found")
        user = self._request("GET", "/users/" + quote(project_user_id, safe=""))
        if not isinstance(user, dict) or user.get("id") != project_user_id:
            raise ProjectUserUnknown("Project account not found")
        if user.get("delete_at"):
            raise ProjectUserUnknown("Project account is unavailable")
        return user

    def is_admin(self, user):
        roles = user.get("roles", "") if isinstance(user, dict) else ""
        return bool(set(roles.split()).intersection(self.admin_roles))

    def require_admin(self, project_user_id):
        user = self.get_user(project_user_id)
        if not self.is_admin(user):
            raise ProjectUserNotAdmin("Project account is not an administrator")
        return user

    def require_member(self, project_user_id):
        return self.get_user(project_user_id)

    def send_direct_message(self, project_user_id, message):
        if not message or any(char in message for char in "\r\n\0"):
            raise ProjectAPIError("Project message is invalid")
        channel = self._request("POST", "/channels/direct",
                                [self.bot_user_id, project_user_id])
        channel_id = channel.get("id") if isinstance(channel, dict) else ""
        if not channel_id:
            raise ProjectAPIError("Project DM channel was not created")
        self._request("POST", "/posts", {"channel_id": channel_id, "message": message})
        return True
