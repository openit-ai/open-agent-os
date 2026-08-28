"""Personal Wiki — vault paths, extraction, Obsidian import.

Lazy imports throughout — no hard deps at import time.
"""
from __future__ import annotations

# Lazy re-exports — import only when accessed to avoid hard dep failures
def __getattr__(name: str):
    if name in ("vault", "extractor", "importer"):
        import importlib
        return importlib.import_module(f"personal_wiki.{name}")
    # vault symbols
    if name in (
        "get_vault_root", "get_journal_dir", "get_notes_dir", "get_attachments_dir",
        "ensure_vault_dirs", "journal_file_for_date", "append_journal", "upsert_note", "vault_path",
    ):
        import importlib
        mod = importlib.import_module("personal_wiki.vault")
        return getattr(mod, name)
    if name in (
        "extract_text", "extract_pdf", "extract_docx", "extract_xlsx", "extract_pptx",
        "extract_image", "extract_attachment", "SUPPORTED_EXTENSIONS",
    ):
        import importlib
        mod = importlib.import_module("personal_wiki.extractor")
        return getattr(mod, name)
    if name in ("import_obsidian_vault", "bulk_import", "ObsidianImporter"):
        import importlib
        mod = importlib.import_module("personal_wiki.importer")
        return getattr(mod, name)
    raise AttributeError(f"module personal_wiki has no attribute {name!r}")

__all__ = [
    "vault", "extractor", "importer",
    "get_vault_root", "get_journal_dir", "get_notes_dir", "get_attachments_dir",
    "ensure_vault_dirs", "journal_file_for_date", "append_journal", "upsert_note", "vault_path",
    "extract_text", "extract_pdf", "extract_docx", "extract_xlsx", "extract_pptx",
    "extract_image", "extract_attachment", "SUPPORTED_EXTENSIONS",
    "import_obsidian_vault", "bulk_import", "ObsidianImporter",
]
