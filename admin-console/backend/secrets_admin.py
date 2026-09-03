"""Canonical secrets status surface — metadata only, never values (admin-console/backend/secrets_admin.py).

GET  /v1/secrets/status          — existence/length/rotation-need per canonical key (auth)
GET  /v1/secrets/rotation-guide  — rotation guide + confirmation checklist (auth)

Canonical keys (installer-generated, SECURITY.md):
  JWT_SIGNING_KEY, AUDIT_SIGNING_KEY, ADMIN_JWT_SECRET, OAOS_ENCRYPTION_KEY

HARD RULES (P2, additive-only):
- Values are NEVER returned, NEVER logged, and NEVER written by this module.
  Responses carry only: name, configured (bool), length (int), source_env
  (env var name only), rotation_needed (bool), reason.
- There is deliberately NO rotation-execution endpoint. Rotation is performed
  via the installer/host env + restart; the console only shows the guide and a
  client-side confirmation checklist. Auth/vault/encryption code is untouched.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

try:
    from .auth import AdminUser, get_current_admin
except ImportError:
    from auth import AdminUser, get_current_admin  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/secrets", tags=["secrets"])

MIN_HEALTHY_LENGTH = 32

# Canonical key -> env candidates (primary first). Only the NAME of the env var
# that provided the value is reported; the value itself never leaves the host.
CANONICAL_SECRETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("JWT_SIGNING_KEY", ("JWT_SIGNING_KEY", "OAOS_JWT_SIGNING_KEY", "OAOS_SIGNING_KEY")),
    ("AUDIT_SIGNING_KEY", ("AUDIT_SIGNING_KEY", "OAOS_AUDIT_SIGNING_KEY")),
    ("ADMIN_JWT_SECRET", ("ADMIN_JWT_SECRET",)),
    ("OAOS_ENCRYPTION_KEY", ("OAOS_ENCRYPTION_KEY", "VAULT_ENCRYPTION_KEY", "OAOS_VAULT_KEY")),
)

# Known placeholder/dev values (compared in-memory only, never returned/logged).
_WEAK_VALUES = frozenset({
    "",
    "change_me",
    "change_me_admin_jwt_32b_minimum",
    "change_me_32_byte_base64_enc_key==",
    "dev-only-change-me",
    "dev-llm-provider-vault-key-please-change-32b",
    "test",
    "testing",
    "secret",
    "password",
})


def _inspect(canonical: str, candidates: tuple[str, ...]) -> dict:
    found_env: str | None = None
    length = 0
    configured = False
    weak = False
    for env_name in candidates:
        raw = os.environ.get(env_name, "")
        if raw and raw.strip():
            found_env = env_name
            length = len(raw.strip())
            configured = True
            weak = raw.strip().lower() in _WEAK_VALUES or raw.strip().startswith("CHANGE_ME")
            break
    if not configured:
        return {"name": canonical, "configured": False, "length": 0,
                "source_env": None, "rotation_needed": True,
                "reason": "not configured (missing env)"}
    if weak:
        return {"name": canonical, "configured": True, "length": length,
                "source_env": found_env, "rotation_needed": True,
                "reason": "placeholder/dev default value in use"}
    if length < MIN_HEALTHY_LENGTH:
        return {"name": canonical, "configured": True, "length": length,
                "source_env": found_env, "rotation_needed": True,
                "reason": f"too short (< {MIN_HEALTHY_LENGTH} chars)"}
    return {"name": canonical, "configured": True, "length": length,
            "source_env": found_env, "rotation_needed": False,
            "reason": "present with healthy length"}


@router.get("/status")
def secrets_status(admin: AdminUser = Depends(get_current_admin)) -> dict:
    items = [_inspect(name, cands) for name, cands in CANONICAL_SECRETS]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": items,
        "rotation_needed_count": sum(1 for i in items if i["rotation_needed"]),
        "note": ("Values are never returned by this API (lengths only). "
                 "Rotation is performed via installer/host env + restart — "
                 "see GET /v1/secrets/rotation-guide. This console cannot rotate secrets."),
    }


@router.get("/rotation-guide")
def secrets_rotation_guide(admin: AdminUser = Depends(get_current_admin)) -> dict:
    return {
        "overview": ("Canonical secrets are installer-generated and live in host "
                     "environment. The admin console never reads values back and "
                     "cannot execute rotation. Follow the steps below on the host, "
                     "then restart services."),
        "steps": [
            {"order": 1, "title": "Generate a new value",
             "detail": "Use a CSPRNG (e.g. `openssl rand -base64 48`) with at least 32 characters of entropy. Do it on the host, never in the browser."},
            {"order": 2, "title": "Update the host environment",
             "detail": "Set the corresponding env var (see source_env in /v1/secrets/status) via the installer or host env file. Keep the old value until cutover if the deployment supports overlap."},
            {"order": 3, "title": "Restart dependent services",
             "detail": "Restart admin-api and any service verifying these keys (auth/audit/vault consumers) so the new value takes effect."},
            {"order": 4, "title": "Verify",
             "detail": "Re-open this page: the key must show configured=true with the new length and rotation_needed=false. Confirm login and audit-chain verification still succeed."},
            {"order": 5, "title": "Retire the old value",
             "detail": "Remove the old value from shell history, backups, and shared notes. Record the rotation date in your ops log."},
        ],
        "checklist": [
            {"id": "new_value_generated", "label": "I generated a new value with a CSPRNG (32+ chars) on the host"},
            {"id": "host_env_updated", "label": "I updated the host env / installer input for the target key"},
            {"id": "services_restarted", "label": "I restarted the dependent services"},
            {"id": "status_rechecked", "label": "I re-checked /v1/secrets/status and login + audit verification pass"},
            {"id": "old_value_retired", "label": "I retired the old value from history/backups/notes"},
        ],
        "executes_rotation": False,
        "note": "Confirmation checklist is client-side only; checking items performs no server-side change.",
    }
