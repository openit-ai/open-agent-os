"""End-to-end verification for Personal Wiki — vault + extractor + importer + watermark.

Covers:
 1) vault append + journal file creation
 2) extractor dispatch for txt/md (and unsupported/edge)
 3) importer bulk copy (Obsidian vault simulation)
 4) consolidate watermark read/write (and consolidate_journal stub)

Uses tmp_path / monkeypatch — no external services, no DB.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _ensure_personal_wiki_package():
    """Fix sys.modules pollution: test_personal_wiki registers personal_wiki as file module."""
    mod = sys.modules.get("personal_wiki")
    if mod is not None and not hasattr(mod, "__path__"):
        if "admin_personal_wiki" not in sys.modules:
            sys.modules["admin_personal_wiki"] = mod
        del sys.modules["personal_wiki"]
        for k in list(sys.modules.keys()):
            if k.startswith("personal_wiki."):
                del sys.modules[k]
    if "personal_wiki" not in sys.modules:
        root = Path(__file__).resolve().parents[1]
        pkg = str(root / "packages" / "personal-wiki")
        if pkg not in sys.path:
            sys.path.insert(0, pkg)


def test_e2e_vault_append_and_journal_creation(tmp_path, monkeypatch):
    """vault append_journal creates journal/YYYY-MM-DD.md with frontmatter and idempotent append."""
    _ensure_personal_wiki_package()
    from personal_wiki.vault import append_journal, journal_file_for_date, get_vault_root

    vault_root = tmp_path / "vault1"
    monkeypatch.setenv("OAOS_WIKI_VAULT", str(vault_root))

    # sanity: get_vault_root respects env
    assert get_vault_root() == vault_root.resolve()

    trace1 = f"trace-{uuid.uuid4().hex[:8]}"
    trace2 = f"trace-{uuid.uuid4().hex[:8]}"
    when = datetime(2026, 8, 28, 9, 30, tzinfo=timezone.utc)

    jfile = append_journal(trace1, "web_search", {"q": "hello world", "results": ["a", "b"]}, vault_root=vault_root, when=when)
    assert jfile is not None
    assert jfile.exists()
    # file should be journal/YYYY-MM-DD.md (flat format, check date part)
    assert "2026-08-28" in jfile.name
    # also verify journal_file_for_date helper
    expected = journal_file_for_date(when.date(), vault_root)
    assert jfile == expected

    content = jfile.read_text(encoding="utf-8")
    assert trace1 in content
    assert "web_search" in content
    assert "hello world" in content
    # frontmatter marker
    assert content.startswith("---")

    # Second append same day should append with separator, not overwrite
    jfile2 = append_journal(trace2, "web_extract", "extracted text body for test", vault_root=vault_root, when=when)
    assert jfile2 == jfile
    content2 = jfile.read_text(encoding="utf-8")
    assert trace1 in content2 and trace2 in content2
    assert "web_extract" in content2
    assert content2.count("trace_id") >= 2
    # separator present after second write
    assert "---" in content2

    # truncation: large result should be truncated (default 4000)
    big = "x" * 6000
    jfile3 = append_journal("trace-big", "tool_big", big, vault_root=vault_root, when=when)
    assert jfile3 == jfile
    big_content = jfile.read_text(encoding="utf-8")
    assert "truncated" in big_content


def test_e2e_extractor_dispatch_txt_md_and_importer_bulk_copy(tmp_path, monkeypatch):
    """Extractor dispatch for txt/md and importer bulk copy from simulated Obsidian vault."""
    _ensure_personal_wiki_package()
    from personal_wiki.extractor import extract_text
    from personal_wiki.importer import import_obsidian_vault

    vault_root = tmp_path / "vault2"
    monkeypatch.setenv("OAOS_WIKI_VAULT", str(vault_root))

    # --- extractor dispatch ---
    txt_file = tmp_path / "sample.txt"
    txt_file.write_text("Hello TXT world — extractor dispatch test", encoding="utf-8")
    out_txt = extract_text(txt_file)
    assert "Hello TXT" in out_txt

    md_file = tmp_path / "sample.md"
    md_file.write_text("# Hello MD\n\nThis is markdown content.", encoding="utf-8")
    out_md = extract_text(md_file)
    assert "Hello MD" in out_md

    # unsupported extension should still return stub string, not raise
    bin_file = tmp_path / "sample.unknown_xyz"
    bin_file.write_text("binary-ish", encoding="utf-8")
    out_unknown = extract_text(bin_file)
    assert isinstance(out_unknown, str) and len(out_unknown) > 0

    # missing file returns not found message
    out_missing = extract_text(tmp_path / "nope.txt")
    assert "not found" in out_missing.lower()

    # --- importer bulk copy ---
    # Simulate Obsidian vault source
    obsidian_src = tmp_path / "obsidian_src"
    obsidian_src.mkdir()
    (obsidian_src / "note1.md").write_text("# Note 1\nContent A", encoding="utf-8")
    (obsidian_src / "folder").mkdir()
    (obsidian_src / "folder" / "note2.md").write_text("# Note 2\nContent B", encoding="utf-8")
    (obsidian_src / "folder" / "attach.txt").write_text("Attachment text content", encoding="utf-8")
    # .obsidian folder should be skipped
    (obsidian_src / ".obsidian").mkdir()
    (obsidian_src / ".obsidian" / "config.json").write_text("{}", encoding="utf-8")
    (obsidian_src / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal png header

    stats = import_obsidian_vault(obsidian_src, vault_root=vault_root, copy_attachments=True, extract_text=False)
    assert stats["notes_copied"] == 2, stats
    assert stats["attachments_copied"] >= 1, stats  # attach.txt + image.png
    assert stats["errors"] == [], stats
    # skipped should be 0 on first import
    assert stats["skipped"] == 0

    # Verify notes copied preserving relative path
    assert (vault_root / "notes" / "note1.md").exists()
    assert (vault_root / "notes" / "folder" / "note2.md").exists()
    assert (vault_root / "notes" / "folder" / "note2.md").read_text(encoding="utf-8").find("Content B") != -1

    # Attachments copied
    assert (vault_root / "attachments" / "folder" / "attach.txt").exists()
    assert (vault_root / "attachments" / "image.png").exists()

    # Obsidian meta skipped
    assert not (vault_root / "notes" / ".obsidian" / "config.json").exists()

    # Second import without overwrite should skip all
    stats2 = import_obsidian_vault(obsidian_src, vault_root=vault_root, copy_attachments=True, extract_text=False)
    assert stats2["notes_copied"] == 0
    assert stats2["skipped"] >= 2

    # With extract_text=True, companion .md notes created for attachments (best-effort)
    vault_root3 = tmp_path / "vault3"
    stats3 = import_obsidian_vault(obsidian_src, vault_root=vault_root3, copy_attachments=True, extract_text=True, max_chars=8000)
    assert stats3["notes_copied"] == 2
    # companion md for attach.txt
    comp = vault_root3 / "notes" / "folder" / "attach.txt.md"
    assert comp.exists()
    assert "Attachment text" in comp.read_text(encoding="utf-8") or "source" in comp.read_text(encoding="utf-8")


def test_e2e_consolidate_watermark_read_write(tmp_path, monkeypatch):
    """Consolidate watermark read/write round-trip and consolidate_journal integration."""
    _ensure_personal_wiki_package()
    from personal_wiki.consolidate import read_watermark, write_watermark, watermark_path, consolidate_journal
    from personal_wiki.vault import append_journal

    vault_root = tmp_path / "vault_wm"
    monkeypatch.setenv("OAOS_WIKI_VAULT", str(vault_root))

    # Initially no watermark
    assert read_watermark(vault_root) is None
    assert watermark_path(vault_root).name == ".consolidate_watermark"

    # Write and read back
    p1 = write_watermark("2026-08-28T00:00:00+00:00", vault_root=vault_root)
    assert p1 is not None and p1.exists()
    assert read_watermark(vault_root) == "2026-08-28T00:00:00+00:00"

    # Overwrite
    p2 = write_watermark("trace-wm-12345", vault_root=vault_root)
    assert p2 == p1
    assert read_watermark(vault_root) == "trace-wm-12345"

    # Also readable via vault_root env fallback (no explicit arg)
    assert read_watermark() == "trace-wm-12345"

    # consolidate_journal should update watermark and create notes/consolidated
    when = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
    append_journal("trace-cons-1", "web_search", "journal entry for consolidation A", vault_root=vault_root, when=when)
    append_journal("trace-cons-2", "web_extract", "journal entry for consolidation B", vault_root=vault_root, when=when)

    prev_wm = read_watermark(vault_root)
    result = consolidate_journal(vault_root=vault_root, target_slug="consolidated/test-merge")
    assert result["merged"] is True
    assert result["note_count"] >= 1
    assert result["watermark"] is not None
    assert result["prev_watermark"] == prev_wm
    # Watermark should have moved
    assert read_watermark(vault_root) == result["watermark"]
    assert read_watermark(vault_root) != prev_wm
    # Note file exists
    note_path = vault_root / "notes" / "consolidated" / "test-merge.md"
    assert note_path.exists()
    note_content = note_path.read_text(encoding="utf-8")
    assert "trace-cons-1" in note_content or "consolidation A" in note_content
