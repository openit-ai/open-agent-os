"""Durable tenant/source checkpoint store for Knowledge Index sync."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .models import ResourceState, SyncCheckpoint


class PersistentCheckpointStore:
    """Synchronous SQL checkpoint store used by the sync worker thread.

    The caller must provide a migration-managed SQLAlchemy Engine. No table is
    created implicitly; a missing schema is an explicit deployment error.
    """

    def __init__(self, engine: Engine, tenant_id: str) -> None:
        if engine is None:
            raise ValueError("engine is required")
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        self.engine = engine
        self.tenant_id = tenant_id.strip()

    def load(self, source_system: str) -> SyncCheckpoint | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT cursor, last_sync_at, resource_states "
                    "FROM knowledge_sync_checkpoints "
                    "WHERE tenant_id = :tenant_id AND source_system = :source_system"
                ),
                {"tenant_id": self.tenant_id, "source_system": source_system},
            ).mappings().first()
        if row is None:
            return None
        raw_states = row["resource_states"] or {}
        if isinstance(raw_states, str):
            raw_states = json.loads(raw_states)
        states = {
            key: ResourceState.from_dict(value)
            for key, value in raw_states.items()
            if isinstance(value, dict)
        }
        return SyncCheckpoint(
            source_system=source_system,
            last_sync_at=row["last_sync_at"] or "",
            cursor=row["cursor"],
            resource_states=states,
        )

    def save(self, checkpoint: SyncCheckpoint) -> None:
        payload = json.dumps(
            {key: value.to_dict() for key, value in checkpoint.resource_states.items()},
            ensure_ascii=False,
        )
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO knowledge_sync_checkpoints "
                    "(tenant_id, source_system, cursor, last_sync_at, resource_states, updated_at) "
                    "VALUES (:tenant_id, :source_system, :cursor, :last_sync_at, :resource_states, :updated_at) "
                    "ON CONFLICT (tenant_id, source_system) DO UPDATE SET "
                    "cursor = excluded.cursor, last_sync_at = excluded.last_sync_at, "
                    "resource_states = excluded.resource_states, updated_at = excluded.updated_at"
                ),
                {
                    "tenant_id": self.tenant_id,
                    "source_system": checkpoint.source_system,
                    "cursor": checkpoint.cursor,
                    "last_sync_at": checkpoint.last_sync_at,
                    "resource_states": payload,
                    "updated_at": now,
                },
            )

    def get_or_create(self, source_system: str) -> SyncCheckpoint:
        return self.load(source_system) or SyncCheckpoint(source_system=source_system)
