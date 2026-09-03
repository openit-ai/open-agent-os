from __future__ import annotations

from pathlib import Path


def test_checkpoint_migration_is_next_after_profile_behavioral():
    text = Path("alembic/versions/018_knowledge_sync_checkpoints.py").read_text()
    assert 'revision = "018_knowledge_sync_checkpoints"' in text
    assert 'down_revision = "017_profile_behavioral"' in text
    assert 'knowledge_sync_checkpoints' in text
