"""Read-only lookup of administrator-approved Mattermost user mappings."""
from __future__ import annotations

import os
from typing import Any


def _normalize_user(value: str) -> str:
    raw = (value or "").strip()
    return raw if raw.startswith("employee:") else f"employee:{raw}"


def lookup_registered_owner(tenant_id: str, user_id: str) -> dict[str, str] | None:
    """Return an active admin mapping or None; never infer an owner in production."""
    principal = _normalize_user(user_id)
    database_url = (os.getenv("OAOS_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    if not database_url:
        return None
    try:
        from sqlalchemy import create_engine, text
        url = database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as connection:
            # Mattermost ingress supplies the canonical employee principal.
            # Match the persisted mapping by principal/agent/username, and keep
            # the query independent of the optional mm_user_id column shape.
            row = connection.execute(
                text(
                    "SELECT mm_user_id, mm_username, employee_principal, agent_id, status "
                    "FROM admin_user_mappings "
                    "WHERE status = 'active' AND (employee_principal = :principal OR agent_id = :agent_id OR mm_username = :username) "
                    "LIMIT 1"
                ),
                {
                    "principal": principal,
                    "agent_id": f"agent:assistant:{principal.split(':', 1)[-1]}",
                    "username": principal.split(":", 1)[-1],
                },
            ).mappings().first()
        engine.dispose()
        if not row:
            return None
        return {
            "mm_user_id": str(row.get("mm_user_id") or ""),
            "mm_username": str(row.get("mm_username") or ""),
            "employee_principal": str(row.get("employee_principal") or principal),
            "agent_id": str(row.get("agent_id") or ""),
            "status": str(row.get("status") or ""),
        }
    except Exception:
        # Production caller treats an unavailable mapping source as unregistered.
        return None
