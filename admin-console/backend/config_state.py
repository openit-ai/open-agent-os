"""Shared additive configuration apply-state fields.

The helpers in this module derive metadata from existing configuration sources.
They do not persist a second copy of configuration or create database objects.
Only already-redacted public configuration dictionaries may be passed here.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def public_revision(namespace: str, config: dict[str, Any]) -> str:
    """Return a stable, non-reversible revision of a public config view."""
    payload = json.dumps(
        {"namespace": namespace, "config": config},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "cfg_" + hashlib.sha256(payload).hexdigest()[:16]


def apply_state_fields(
    namespace: str,
    config: dict[str, Any],
    source: str,
    *,
    effective_config: dict[str, Any] | None = None,
    persisted: bool | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Build the §5 common fields without changing legacy response fields."""
    applied = source == "env"
    config_revision = public_revision(namespace, config)
    effective_revision = (
        config_revision
        if applied
        else public_revision(namespace, effective_config or {})
    )
    timestamp = updated_at or utc_now_iso()
    return {
        "persisted": source in {"db", "env", "in-memory"} if persisted is None else persisted,
        "applied": applied,
        "config_revision": config_revision,
        "effective_revision": effective_revision,
        "requires_restart": not applied,
        "apply_strategy": "immediate" if applied else "restart_required",
        "updated_at": timestamp,
        **({"applied_at": timestamp} if applied else {}),
    }


def config_response(
    namespace: str,
    config: dict[str, Any],
    source: str,
    note: str,
    *,
    effective_config: dict[str, Any] | None = None,
    persisted: bool | None = None,
) -> dict[str, Any]:
    """Return a legacy config response with additive common metadata."""
    return {
        **config,
        "source": source,
        **apply_state_fields(
            namespace,
            config,
            source,
            effective_config=effective_config,
            persisted=persisted,
        ),
        "note": note,
    }
