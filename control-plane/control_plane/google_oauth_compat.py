"""Compatibility aliases exposing the canonical OAOS Google OAuth flow at the
``/v1/oauth/google/*`` route shape.

Canonical implementation lives in :mod:`control_plane.google_oauth`
(``POST /v1/google/oauth/authorize``, ``GET /v1/google/oauth/callback``,
``GET /v1/google/oauth/status``, ``POST /v1/google/oauth/revoke``).
This module adds thin aliases required by the OAOS integration contract::

    GET  /v1/oauth/google/start    (authenticated owner → auth_url + state)
    GET  /v1/oauth/google/callback (Google redirect: code + state only)
    POST /v1/oauth/google/callback (same, JSON body)
    GET  /v1/oauth/google/status   (authenticated owner → metadata only)
    POST /v1/oauth/google/revoke   (authenticated owner → revoke/delete)

No logic is duplicated here — every handler delegates to the canonical
functions, so owner binding, one-time expiring state, PKCE, vault storage,
and fail-closed semantics are identical on both route trees. Responses carry
``secret_ref`` + metadata only; tokens are never returned.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field

try:
    from . import google_oauth as _go
except ImportError:  # pragma: no cover
    from control_plane import google_oauth as _go  # type: ignore

router = APIRouter(prefix="/v1/oauth/google", tags=["oauth-google"])


class CallbackBody(BaseModel):
    code: str = Field(default="")
    state: str = Field(default="")

    model_config = {"extra": "forbid"}


class RevokeBody(BaseModel):
    delegation_id: str = Field(default="")

    model_config = {"extra": "forbid"}


@router.get("/start")
async def oauth_start(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    tenant_id: str | None = None,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
    session_id: str | None = None,
    scope: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    """Authenticated owner → Google consent URL + opaque state. No secrets."""
    out = await _go.authorize(
        _go.AuthorizeRequest(
            session_id=session_id,
            scopes=(scope or "").split() or None,
            expected_email=email,
        ),
        authorization=authorization,
        x_user_id=x_user_id,
        x_tenant_id=tenant_id or x_tenant_id,
        x_agent_id=x_agent_id,
    )
    # Contract shape: auth_url + opaque state/request ID.
    return {
        "auth_url": out["authorization_url"],
        "authorization_url": out["authorization_url"],
        "state": out["state"],
        "expires_in": out["expires_in"],
        "scopes": out["scopes"],
    }


@router.get("/callback")
async def oauth_callback_get(
    code: str = "",
    state: str = "",
    error: str | None = None,
) -> dict[str, Any]:
    """Google redirect target. State is the only lookup key."""
    return await _go.callback(state=state, code=code, error=error)


@router.post("/callback")
async def oauth_callback_post(body: CallbackBody) -> dict[str, Any]:
    """Same as GET callback for non-redirect clients."""
    return await _go.callback(state=body.state, code=body.code, error=None)


@router.get("/status")
async def oauth_status(
    delegation_id: str = "",
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    tenant_id: str | None = None,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> dict[str, Any]:
    """Authenticated owner → binding metadata only."""
    return await _go.status(
        delegation_id=delegation_id,
        authorization=authorization,
        x_user_id=x_user_id,
        x_tenant_id=tenant_id or x_tenant_id,
        x_agent_id=x_agent_id,
    )


@router.post("/revoke")
async def oauth_revoke(
    body: RevokeBody,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    tenant_id: str | None = None,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> dict[str, Any]:
    """Authenticated owner → explicit revoke (Google + vault + delegation)."""
    return await _go.revoke(
        _go.RevokeRequest(delegation_id=body.delegation_id),
        authorization=authorization,
        x_user_id=x_user_id,
        x_tenant_id=tenant_id or x_tenant_id,
        x_agent_id=x_agent_id,
    )
