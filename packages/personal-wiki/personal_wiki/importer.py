"""Obsidian bulk import wrapper — copies markdown vault into personal wiki.

- Walks Obsidian vault (source_dir) recursively
- Copies .md files to vault notes dir preserving relative path
- For attachments (pdf/docx/xlsx/pptx/images), optionally extracts text via extractor
  and creates companion .md notes with frontmatter
- Best-effort, no hard deps, lazy imports
"""
from __future__ import annotations

import os
import shutil
import re
from pathlib import Path
from typing import Any

# Lazy import vault/extractor inside functions to avoid circular + hard deps


def _get_vault():
    try:
        from personal_wiki import vault as _v  # type: ignore
        return _v
    except Exception:
        try:
            from personal_wiki.vault import (  # type: ignore
                get_vault_root, get_notes_dir, get_attachments_dir, ensure_vault_dirs,
            )
            import types
            m = types.SimpleNamespace(
                get_vault_root=get_vault_root,
                get_notes_dir=get_notes_dir,
                get_attachments_dir=get_attachments_dir,
                ensure_vault_dirs=ensure_vault_dirs,
            )
            return m
        except Exception:
            return None


def _extract_text_lazy(path: Path, max_chars: int = 8000) -> str:
    try:
        from personal_wiki.extractor import extract_text  # type: ignore
        return extract_text(path, max_chars=max_chars)
    except Exception as e:
        return f"[extraction failed: {e}]"


def _is_obsidian_meta(p: Path) -> bool:
    # skip .obsidian, .trash, etc.
    parts = p.parts
    for part in parts:
        if part in (".obsidian", ".trash", ".git"):
            return True
    return False


def import_obsidian_vault(
    source_dir: Path | str,
    vault_root: Path | str | None = None,
    copy_attachments: bool = True,
    extract_text: bool = False,
    max_chars: int = 8000,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Bulk import Obsidian vault into personal wiki.

    Args:
        source_dir: Obsidian vault source directory
        vault_root: personal wiki vault root (default from vault.get_vault_root())
        copy_attachments: copy non-md attachments to vault attachments dir
        extract_text: if True, extract attachment text and create companion .md in notes
        max_chars: max chars for extraction
        overwrite: overwrite existing notes

    Returns:
        dict with counts: {notes_copied, attachments_copied, attachments_extracted, errors, skipped}
    """
    src = Path(source_dir).expanduser().resolve()
    if not src.exists() or not src.is_dir():
        return {"notes_copied": 0, "attachments_copied": 0, "attachments_extracted": 0,
                "errors": [f"source not found: {src}"], "skipped": 0}

    # vault setup (lazy)
    vault = _get_vault()
    if vault is not None:
        try:
            root = Path(vault_root) if vault_root else vault.get_vault_root()  # type: ignore
            vault.ensure_vault_dirs(root)  # type: ignore
            notes_dir = vault.get_notes_dir(root)  # type: ignore
            attach_dir = vault.get_attachments_dir(root)  # type: ignore
        except Exception:
            root = Path(vault_root) if vault_root else src.parent / "wiki-vault"
            notes_dir = root / "notes"
            attach_dir = root / "attachments"
            try:
                root.mkdir(parents=True, exist_ok=True)
                notes_dir.mkdir(parents=True, exist_ok=True)
                attach_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
    else:
        root = Path(vault_root) if vault_root else src.parent / "wiki-vault"
        notes_dir = root / "notes"
        attach_dir = root / "attachments"
        try:
            notes_dir.mkdir(parents=True, exist_ok=True)
            attach_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    stats: dict[str, Any] = {
        "notes_copied": 0,
        "attachments_copied": 0,
        "attachments_extracted": 0,
        "errors": [],
        "skipped": 0,
        "vault_root": str(root),
    }

    # extensions
    try:
        from personal_wiki.extractor import SUPPORTED_EXTENSIONS  # type: ignore
        attach_exts = set(SUPPORTED_EXTENSIONS)
    except Exception:
        attach_exts = {".pdf", ".docx", ".xlsx", ".pptx", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}

    md_ext = {".md", ".markdown"}

    for p in src.rglob("*"):
        if not p.is_file():
            continue
        if _is_obsidian_meta(p):
            continue
        rel = p.relative_to(src)
        ext = p.suffix.lower()

        # Obsidian notes
        if ext in md_ext:
            dest = notes_dir / rel
            try:
                if dest.exists() and not overwrite:
                    stats["skipped"] += 1
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(p), str(dest))
                stats["notes_copied"] += 1
            except Exception as e:
                stats["errors"].append(f"{rel}: {e}")
            continue

        # attachments
        if ext in attach_exts:
            if not copy_attachments and not extract_text:
                continue
            # copy to attachments dir preserving relative path
            if copy_attachments:
                dest_a = attach_dir / rel
                try:
                    if not dest_a.exists() or overwrite:
                        dest_a.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(p), str(dest_a))
                        stats["attachments_copied"] += 1
                    else:
                        stats["skipped"] += 1
                except Exception as e:
                    stats["errors"].append(f"{rel}: {e}")
            if extract_text:
                try:
                    text = _extract_text_lazy(p, max_chars=max_chars)
                    # create companion md in notes: same rel but + .md
                    comp_rel = rel.with_suffix(rel.suffix + ".md")
                    dest_md = notes_dir / comp_rel
                    # add frontmatter
                    from datetime import datetime, timezone
                    fm = f"---\nsource: \"{p.name}\"\noriginal: \"{rel}\"\nimported: \"{datetime.now(timezone.utc).isoformat()}\"\n---\n\n"
                    try:
                        if not dest_md.exists() or overwrite:
                            dest_md.parent.mkdir(parents=True, exist_ok=True)
                            dest_md.write_text(fm + text, encoding="utf-8")
                            stats["attachments_extracted"] += 1
                        else:
                            stats["skipped"] += 1
                    except Exception as e:
                        stats["errors"].append(f"{rel} extract write: {e}")
                except Exception as e:
                    stats["errors"].append(f"{rel} extract: {e}")
            continue

        # unknown files: copy if copy_attachments
        if copy_attachments and ext and ext not in md_ext:
            dest_a = attach_dir / rel
            try:
                if not dest_a.exists() or overwrite:
                    dest_a.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(p), str(dest_a))
                    stats["attachments_copied"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                stats["errors"].append(f"{rel}: {e}")

    return stats


# Wrapper aliases
bulk_import = import_obsidian_vault
import_obsidian = import_obsidian_vault


class ObsidianImporter:
    """Class wrapper for Obsidian bulk import (for dependency injection / testing)."""

    def __init__(
        self,
        source_dir: Path | str,
        vault_root: Path | str | None = None,
        copy_attachments: bool = True,
        extract_text: bool = False,
        max_chars: int = 8000,
        overwrite: bool = False,
    ):
        self.source_dir = Path(source_dir)
        self.vault_root = Path(vault_root) if vault_root else None
        self.copy_attachments = copy_attachments
        self.extract_text = extract_text
        self.max_chars = max_chars
        self.overwrite = overwrite
        self.last_result: dict[str, Any] | None = None

    def run(self) -> dict[str, Any]:
        self.last_result = import_obsidian_vault(
            self.source_dir,
            vault_root=self.vault_root,
            copy_attachments=self.copy_attachments,
            extract_text=self.extract_text,
            max_chars=self.max_chars,
            overwrite=self.overwrite,
        )
        return self.last_result

    # alias for compat
    def import_vault(self) -> dict[str, Any]:
        return self.run()

    def bulk_import(self) -> dict[str, Any]:
        return self.run()
