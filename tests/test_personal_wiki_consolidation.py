"""Watermark logic test for Personal Wiki consolidation scheduler.

Uses file-based import to avoid pollution from admin_console personal_wiki alias.
"""
from __future__ import annotations

import json
import sys
import tempfile
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSOLIDATE_FILE = ROOT / "packages" / "personal-wiki" / "personal_wiki" / "consolidate.py"


def _load_consolidate():
    """Load consolidate.py directly via spec to avoid sys.modules['personal_wiki'] pollution."""
    # temporarily ensure packages/personal-wiki on path for vault dependency
    pkg_root = str(ROOT / "packages" / "personal-wiki")
    added = pkg_root not in sys.path
    if added:
        sys.path.insert(0, pkg_root)
    try:
        # Use a unique module name to avoid collision with 'personal_wiki' alias
        spec = importlib.util.spec_from_file_location("personal_wiki_consolidate_under_test", str(CONSOLIDATE_FILE))
        mod = importlib.util.module_from_spec(spec)  # type: ignore
        spec.loader.exec_module(mod)  # type: ignore
        return mod
    finally:
        # keep path for other tests
        pass


def test_consolidation_watermark_byte_offsets_and_cap():
    mod = _load_consolidate()
    WATERMARK = mod.WATERMARK
    CAP_BYTES = mod.CAP_BYTES

    assert WATERMARK == ".consolidate.json"
    assert CAP_BYTES == 14 * 1024 == 14336

    tmp = Path(tempfile.mkdtemp())
    jdir = tmp / "journal"
    jdir.mkdir(parents=True)

    f1 = jdir / "2026-08-27.md"
    content1 = "journal entry with enough substantive text to pass signal gate. " * 20
    f1.write_text(content1, encoding="utf-8")

    text, offsets = mod.gather_new_journal(ws_id=None, vault_root=tmp)
    assert len(text) > 0
    assert "journal/2026-08-27.md" in offsets
    assert offsets["journal/2026-08-27.md"] == f1.stat().st_size
    assert len(text.encode("utf-8")) <= CAP_BYTES

    mod._save_json_watermark(offsets, vault_root=tmp)
    assert (tmp / WATERMARK).exists()
    data = json.loads((tmp / WATERMARK).read_text(encoding="utf-8"))
    assert data["journal/2026-08-27.md"] == offsets["journal/2026-08-27.md"]
    assert "_updated_at" in data

    text2, offsets2 = mod.gather_new_journal(ws_id=None, vault_root=tmp)
    assert text2 == ""

    with f1.open("a", encoding="utf-8") as fh:
        extra = "\nnew journal line with enough content to be detected as new bytes for incremental watermark test "
        fh.write(extra)
    text3, offsets3 = mod.gather_new_journal(ws_id=None, vault_root=tmp)
    assert "new journal line" in text3
    assert offsets3["journal/2026-08-27.md"] == f1.stat().st_size
    assert offsets3["journal/2026-08-27.md"] > offsets["journal/2026-08-27.md"]

    # CAP 14KB test
    tmp2 = Path(tempfile.mkdtemp())
    jdir2 = tmp2 / "journal"
    jdir2.mkdir(parents=True)
    (jdir2 / "big.md").write_text("y" * 30000, encoding="utf-8")
    text_big, _ = mod.gather_new_journal(ws_id=None, vault_root=tmp2)
    assert len(text_big.encode("utf-8")) <= CAP_BYTES + 100
    import shutil
    shutil.rmtree(tmp2, ignore_errors=True)

    # also verify prompt / scheduler basics
    assert "signal gate" in mod.PROMPT.lower()
    assert "JSON" in mod.PROMPT
    assert "KO" in mod.PROMPT and "EN" in mod.PROMPT
    notes = [{"slug": f"n{i}", "title": f"title {i}"} for i in range(20)]
    p = mod.build_prompt("journal enough content " * 10, recent_notes=notes, lang="ko")
    assert p.count("- n") == 12
    res = mod.register_consolidation_scheduler(ws_ids=None)
    assert res["cron"] == "0 2 * * *"
    assert res["timezone"] == "Asia/Seoul"
