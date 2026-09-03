from pathlib import Path


def test_backfill_uses_single_page_batches_and_durable_checkpoint():
    text = Path("scripts/verify-knowledge-live.py").read_text()
    assert "max_pages=1" in text
    assert "PersistentCheckpointStore" in text
    assert "while batches < 500" in text
    assert "checkpoint_store=checkpoint_store" in text
