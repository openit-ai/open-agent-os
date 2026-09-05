"""Static and isolated checks for the 019 embedding contract migration."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_019_migration_is_after_checkpoint_and_refuses_bad_rows() -> None:
    text = (ROOT / "alembic/versions/019_knowledge_index_embedding_1024.py").read_text()
    assert 'revision = "019_ki_embedding_1024"' in text
    assert 'down_revision = "018_knowledge_sync_checkpoints"' in text
    assert "jsonb_array_length" in text
    assert "embedding::vector(1024)" in text
    assert "older" in text and "NULL" in text
    assert "DROP TABLE" not in text.upper()


def test_runtime_orm_keeps_vector_dimension_contract_explicit() -> None:
    text = (ROOT / "knowledge_index/orm.py").read_text()
    assert "_VECTOR_RUNTIME" in text
    assert "_PgVectorRuntime(1024)" in text
    assert "embedding" in text
