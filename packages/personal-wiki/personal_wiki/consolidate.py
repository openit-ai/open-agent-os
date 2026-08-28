"""Consolidation helpers — watermark (byte-offset) + e2e watermark + notes merge.

Supports two watermark mechanisms:
 1) Byte-offset JSON watermark at <vault_root>/.consolidate.json (14KB cap)
    used by scheduler: gather_new_journal / _save_json_watermark
 2) Simple text watermark at <vault_root>/.consolidate_watermark
    used by e2e tests: read_watermark / write_watermark / consolidate_journal

All file ops best-effort, stdlib only.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Legacy simple text watermark (e2e) — keep compatible with e2e tests
# ---------------------------------------------------------------------------
try:
    from personal_wiki.vault import get_vault_root, ensure_vault_dirs  # type: ignore
except Exception:  # fallback
    def get_vault_root() -> Path:  # type: ignore[no-redef]
        for k in ("OAOS_WIKI_VAULT", "PERSONAL_WIKI_VAULT", "VAULT_ROOT", "PERSONAL_WIKI_ROOT"):
            v = os.getenv(k)
            if v and v.strip():
                return Path(v.strip()).expanduser().resolve()
        return Path.home() / ".open-agent-os" / "wiki-vault"

    def ensure_vault_dirs(vault_root: Path | str | None = None) -> Path:  # type: ignore[no-redef]
        root = Path(vault_root) if vault_root else get_vault_root()
        for d in (root, root / "journal", root / "notes", root / "attachments"):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        return root

_WATERMARK_FILENAME = ".consolidate_watermark"
_WATERMARK_LEGACY = "journal/.watermark"

def _watermark_path(vault_root: Path | str | None = None) -> Path:
    root = Path(vault_root) if vault_root else get_vault_root()
    return root / _WATERMARK_FILENAME

def watermark_path(vault_root: Path | str | None = None) -> Path:
    return _watermark_path(vault_root)

def read_watermark(vault_root: Path | str | None = None) -> str | None:
    p = _watermark_path(vault_root)
    try:
        if p.exists():
            txt = p.read_text(encoding="utf-8").strip()
            return txt if txt else None
    except Exception:
        pass
    try:
        root = Path(vault_root) if vault_root else get_vault_root()
        legacy = root / _WATERMARK_LEGACY
        if legacy.exists():
            txt = legacy.read_text(encoding="utf-8").strip()
            return txt if txt else None
    except Exception:
        pass
    return None

def write_watermark(value: str, vault_root: Path | str | None = None) -> Path | None:
    try:
        root = Path(vault_root) if vault_root else get_vault_root()
        try:
            ensure_vault_dirs(root)
        except Exception:
            pass
        p = root / _WATERMARK_FILENAME
        p.parent.mkdir(parents=True, exist_ok=True)
        v = str(value).strip()
        if not v:
            v = datetime.now(timezone.utc).isoformat()
        p.write_text(v + "\n", encoding="utf-8")
        return p
    except Exception:
        return None

get_consolidate_watermark = read_watermark
set_consolidate_watermark = write_watermark
get_watermark = read_watermark
set_watermark = write_watermark
load_watermark = read_watermark
save_watermark = write_watermark

# ---------------------------------------------------------------------------
# JSON byte-offset watermark — scheduler path
# ---------------------------------------------------------------------------
WATERMARK = ".consolidate.json"
CAP_BYTES = 14 * 1024  # 14336
RECENT_NOTES_LIMIT = 12

PROMPT = """\
You are the Personal Wiki consolidation scheduler. Signal gate: only consolidate when substantive content passes signal gate.
Output must be valid JSON with KO and EN titles.
- Review journal entries, deduplicate, and produce a consolidated note.
- Include KO and EN summaries.
- Mark low-signal entries for skip.
"""

def _json_watermark_path(vault_root: Path | str | None = None) -> Path:
    root = Path(vault_root) if vault_root else get_vault_root()
    return root / WATERMARK

def _load_json_watermark(vault_root: Path | str | None = None) -> dict[str, Any]:
    p = _json_watermark_path(vault_root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}

def _save_json_watermark(offsets: dict[str, Any], vault_root: Path | str | None = None) -> Path | None:
    try:
        root = Path(vault_root) if vault_root else get_vault_root()
        root.mkdir(parents=True, exist_ok=True)
        p = root / WATERMARK
        data = dict(offsets) if isinstance(offsets, dict) else {}
        # keep only journal offsets + metadata
        data["_updated_at"] = datetime.now(timezone.utc).isoformat()
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return p
    except Exception:
        return None

# backward compat alias
save_json_watermark = _save_json_watermark
load_json_watermark = _load_json_watermark

def gather_new_journal(ws_id: str | None = None, vault_root: Path | str | None = None) -> tuple[str, dict[str, int]]:
    """Read new bytes from journal/*.md since last JSON watermark.

    Args:
        ws_id: optional workspace id (currently ignored for path resolution; kept for API compat)
        vault_root: vault root dir; if None, uses get_vault_root()
    Returns:
        (text, offsets) where text is concatenated new bytes (capped at CAP_BYTES), offsets is new watermark dict.
    """
    try:
        root = Path(vault_root) if vault_root else get_vault_root()
        # ws_id handling: if vault is per-workspace layout, could be root/{ws_id}/journal; for now ignore ws_id
        journal_dir = root / "journal"
        prev = _load_json_watermark(root)
        # collect journal files
        files: list[Path] = []
        if journal_dir.exists():
            files = sorted(journal_dir.rglob("*.md"))
        new_parts: list[str] = []
        new_offsets: dict[str, int] = {}
        total_bytes = 0
        for f in files:
            try:
                rel = f.relative_to(root).as_posix()  # "journal/2026-08-27.md"
            except Exception:
                rel = f"journal/{f.name}"
            cur_size = f.stat().st_size
            prev_offset = int(prev.get(rel, 0)) if isinstance(prev.get(rel), (int, float)) else 0
            # if file shrank (rotation), treat as new
            if cur_size < prev_offset:
                prev_offset = 0
            new_offsets[rel] = cur_size
            if cur_size > prev_offset:
                try:
                    # read only new bytes
                    with f.open("rb") as fh:
                        fh.seek(prev_offset)
                        chunk = fh.read()
                        # decode best-effort
                        try:
                            text_chunk = chunk.decode("utf-8")
                        except UnicodeDecodeError:
                            text_chunk = chunk.decode("utf-8", errors="ignore")
                        # cap
                        if total_bytes + len(text_chunk.encode("utf-8")) > CAP_BYTES:
                            remaining = CAP_BYTES - total_bytes
                            if remaining <= 0:
                                break
                            # truncate text to remaining bytes
                            encoded = text_chunk.encode("utf-8")
                            text_chunk = encoded[:remaining].decode("utf-8", errors="ignore")
                        new_parts.append(text_chunk)
                        total_bytes += len(text_chunk.encode("utf-8"))
                        if total_bytes >= CAP_BYTES:
                            break
                except Exception:
                    continue
            if total_bytes >= CAP_BYTES:
                break
        # ensure all files have offset even if not read due to cap? No — only files processed
        # For files not yet capped, ensure offset recorded
        for f in files:
            try:
                rel = f.relative_to(root).as_posix()
            except Exception:
                rel = f"journal/{f.name}"
            if rel not in new_offsets:
                try:
                    new_offsets[rel] = f.stat().st_size
                except Exception:
                    pass
        text = "".join(new_parts)
        return text, new_offsets
    except Exception:
        return "", {}

def build_prompt(journal_text: str, recent_notes: list[dict[str, Any]] | None = None, lang: str = "ko") -> str:
    """Build LLM prompt for consolidation. Truncates recent_notes to RECENT_NOTES_LIMIT."""
    notes = recent_notes or []
    limited = notes[:RECENT_NOTES_LIMIT]
    lines: list[str] = [PROMPT.strip(), "", f"Language: {lang}", "", "Journal context:", journal_text[: CAP_BYTES]]
    if limited:
        lines.append("")
        lines.append("Recent notes:")
        for n in limited:
            slug = n.get("slug", "unknown")
            title = n.get("title", slug)
            lines.append(f"- {slug}: {title}")
    return "\n".join(lines)

def register_consolidation_scheduler(ws_ids: list[str] | None = None) -> dict[str, Any]:
    """Stub scheduler registration — returns cron spec."""
    return {"cron": "0 2 * * *", "timezone": "Asia/Seoul", "ws_ids": ws_ids or [], "enabled": True}

# ---------------------------------------------------------------------------
# consolidate_journal stub — uses simple text watermark
# ---------------------------------------------------------------------------
def consolidate_journal(
    vault_root: Path | str | None = None,
    target_slug: str | None = None,
    since_watermark: bool = True,
) -> dict[str, Any]:
    try:
        root = Path(vault_root) if vault_root else get_vault_root()
        journal_dir = root / "journal"
        notes_dir = root / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        prev = read_watermark(root) if since_watermark else None
        journals: list[Path] = []
        if journal_dir.exists():
            journals = sorted(journal_dir.rglob("*.md"))
        if not journals:
            wm_val = datetime.now(timezone.utc).isoformat()
            write_watermark(wm_val, root)
            return {"merged": False, "watermark": wm_val, "note_path": None, "note_count": 0, "prev_watermark": prev}
        merged_parts: list[str] = []
        for jf in journals:
            try:
                merged_parts.append(f"# {jf.name}\n\n{jf.read_text(encoding='utf-8')}\n")
            except Exception:
                continue
        if not merged_parts:
            wm_val = datetime.now(timezone.utc).isoformat()
            write_watermark(wm_val, root)
            return {"merged": False, "watermark": wm_val, "note_path": None, "note_count": 0, "prev_watermark": prev}
        if target_slug is None:
            target_slug = f"consolidated/{datetime.now(timezone.utc).date().isoformat()}"
        try:
            from personal_wiki.vault import upsert_note  # type: ignore
            note_path = upsert_note(
                target_slug,
                "\n\n---\n\n".join(merged_parts),
                frontmatter={"consolidated_at": datetime.now(timezone.utc).isoformat(), "source": "journal"},
                vault_root=root,
            )
        except Exception:
            slug = target_slug.strip().lstrip("/")
            if not slug.endswith(".md"):
                slug += ".md"
            note_path = notes_dir / slug
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text("\n\n---\n\n".join(merged_parts), encoding="utf-8")
        wm_val = datetime.now(timezone.utc).isoformat()
        write_watermark(wm_val, root)
        return {"merged": True, "watermark": wm_val, "note_path": str(note_path) if note_path else None, "note_count": len(journals), "prev_watermark": prev}
    except Exception as e:
        return {"merged": False, "error": str(e), "watermark": None, "note_path": None, "note_count": 0}
