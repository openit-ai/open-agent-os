from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from knowledge_index.checkpoint import PersistentCheckpointStore
from knowledge_index.models import ResourceState, SyncCheckpoint


def test_checkpoint_round_trip_is_tenant_and_source_scoped():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE knowledge_sync_checkpoints (
                tenant_id VARCHAR(128) NOT NULL,
                source_system VARCHAR(64) NOT NULL,
                cursor TEXT,
                last_sync_at TEXT NOT NULL,
                resource_states JSON NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (tenant_id, source_system)
            )
        """))
    store = PersistentCheckpointStore(engine, "tenant-a")
    checkpoint = SyncCheckpoint(
        source_system="outline",
        cursor="cursor-1",
        last_sync_at=datetime.now(timezone.utc).isoformat(),
        resource_states={"doc-1": ResourceState("hash", "time", "acl-1")},
    )
    store.save(checkpoint)
    loaded = store.load("outline")
    assert loaded is not None
    assert loaded.cursor == "cursor-1"
    assert loaded.resource_states["doc-1"].acl_version == "acl-1"
    assert PersistentCheckpointStore(engine, "tenant-b").load("outline") is None
