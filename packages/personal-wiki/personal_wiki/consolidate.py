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
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ConsolidationError(RuntimeError):
    """Base error for consolidation failures."""

    status_code = 503


class ConsolidationValidationError(ConsolidationError):
    """A watermark or consolidation record is malformed."""

    status_code = 422


class ConsolidationStorageError(ConsolidationError):
    """Durable consolidation storage is unavailable or rejected a write."""


def _is_production() -> bool:
    return any(
        os.getenv(key, "").strip().lower() in ("production", "prod")
        for key in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT")
    )


def _source_name(path: Path, root: Path) -> str:
    """Return only a vault-relative identifier for safe failure metadata."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _path_exists(path: Path) -> bool:
    """Distinguish a missing path from an unavailable filesystem."""
    try:
        path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ConsolidationStorageError("consolidation filesystem is unavailable") from exc
    return True

# ---------------------------------------------------------------------------
# Legacy simple text watermark (e2e) — keep compatible with e2e tests
# ---------------------------------------------------------------------------
try:
    from personal_wiki.vault import get_vault_root, ensure_vault_dirs  # type: ignore
except (ImportError, ModuleNotFoundError):  # fallback
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
            except OSError as exc:
                logger.warning("personal wiki vault directory unavailable: %s", d.name)
                raise ConsolidationStorageError("personal wiki vault directory unavailable") from exc
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
    if _path_exists(p):
        try:
            txt = p.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise ConsolidationStorageError("consolidation watermark is unavailable") from exc
        return txt if txt else None
    root = Path(vault_root) if vault_root else get_vault_root()
    legacy = root / _WATERMARK_LEGACY
    if _path_exists(legacy):
        try:
            txt = legacy.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise ConsolidationStorageError("legacy consolidation watermark is unavailable") from exc
        return txt if txt else None
    return None

def write_watermark(value: str, vault_root: Path | str | None = None) -> Path | None:
    root = Path(vault_root) if vault_root else get_vault_root()
    ensure_vault_dirs(root)
    p = root / _WATERMARK_FILENAME
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        v = str(value).strip()
        if not v:
            v = datetime.now(timezone.utc).isoformat()
        p.write_text(v + "\n", encoding="utf-8")
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise ConsolidationStorageError("consolidation watermark write failed") from exc
    return p

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
    if not _path_exists(p):
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ConsolidationStorageError("JSON consolidation watermark is unavailable") from exc
    except json.JSONDecodeError as exc:
        raise ConsolidationValidationError("JSON consolidation watermark is malformed") from exc
    if not isinstance(data, dict):
        raise ConsolidationValidationError("JSON consolidation watermark must be an object")
    return data

def _save_json_watermark(offsets: dict[str, Any], vault_root: Path | str | None = None) -> Path | None:
    if not isinstance(offsets, dict):
        raise ConsolidationValidationError("JSON consolidation watermark offsets must be an object")
    root = Path(vault_root) if vault_root else get_vault_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        p = root / WATERMARK
        data = dict(offsets)
        data["_updated_at"] = datetime.now(timezone.utc).isoformat()
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise ConsolidationStorageError("JSON consolidation watermark write failed") from exc
    return p

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
    root = Path(vault_root) if vault_root else get_vault_root()
    journal_dir = root / "journal"
    prev = _load_json_watermark(root)
    try:
        files = sorted(journal_dir.rglob("*.md")) if _path_exists(journal_dir) else []
    except OSError as exc:
        raise ConsolidationStorageError("journal source listing failed") from exc

    new_parts: list[str] = []
    new_offsets: dict[str, int] = {}
    total_bytes = 0
    for journal_file in files:
        rel = _source_name(journal_file, root)
        try:
            current_size = journal_file.stat().st_size
        except OSError as exc:
            raise ConsolidationStorageError(f"journal source unavailable: {rel}") from exc
        raw_offset = prev.get(rel, 0)
        if not isinstance(raw_offset, (int, float)) or isinstance(raw_offset, bool):
            raise ConsolidationValidationError(f"journal watermark offset is malformed: {rel}")
        try:
            previous_offset = int(raw_offset)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ConsolidationValidationError(f"journal watermark offset is malformed: {rel}") from exc
        if current_size < previous_offset:
            previous_offset = 0
        new_offsets[rel] = current_size
        if current_size <= previous_offset:
            continue
        try:
            with journal_file.open("rb") as handle:
                handle.seek(previous_offset)
                chunk = handle.read()
        except (OSError, ValueError) as exc:
            raise ConsolidationStorageError(f"journal source read failed: {rel}") from exc
        try:
            text_chunk = chunk.decode("utf-8")
        except UnicodeDecodeError:
            text_chunk = chunk.decode("utf-8", errors="ignore")
        encoded_length = len(text_chunk.encode("utf-8"))
        if total_bytes + encoded_length > CAP_BYTES:
            remaining = CAP_BYTES - total_bytes
            if remaining <= 0:
                break
            text_chunk = text_chunk.encode("utf-8")[:remaining].decode("utf-8", errors="ignore")
        new_parts.append(text_chunk)
        total_bytes += len(text_chunk.encode("utf-8"))
        if total_bytes >= CAP_BYTES:
            break

    for journal_file in files:
        rel = _source_name(journal_file, root)
        if rel not in new_offsets:
            try:
                new_offsets[rel] = journal_file.stat().st_size
            except OSError as exc:
                raise ConsolidationStorageError(f"journal source unavailable: {rel}") from exc
    return "".join(new_parts), new_offsets

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
    root = Path(vault_root) if vault_root else get_vault_root()
    journal_dir = root / "journal"
    notes_dir = root / "notes"
    try:
        notes_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConsolidationStorageError("consolidation notes directory unavailable") from exc
    prev = read_watermark(root) if since_watermark else None
    try:
        journals = sorted(journal_dir.rglob("*.md")) if _path_exists(journal_dir) else []
    except OSError as exc:
        raise ConsolidationStorageError("journal source listing failed") from exc

    def degraded_result(
        reason: str,
        failed_sources: list[str] | None = None,
        note_path: Path | None = None,
    ) -> dict[str, Any]:
        return {
            "merged": False,
            "degraded": True,
            "error_code": reason,
            "watermark": None,
            "note_path": str(note_path) if note_path else None,
            "note_count": 0,
            "failed_count": len(failed_sources or []),
            "failed_sources": failed_sources or [],
            "prev_watermark": prev,
        }

    if not journals:
        wm_val = datetime.now(timezone.utc).isoformat()
        try:
            write_watermark(wm_val, root)
        except ConsolidationStorageError:
            if _is_production():
                raise
            logger.warning("consolidation watermark update degraded with no journal sources")
            return degraded_result("WATERMARK_WRITE_FAILED")
        return {
            "merged": False,
            "degraded": False,
            "watermark": wm_val,
            "note_path": None,
            "note_count": 0,
            "failed_count": 0,
            "failed_sources": [],
            "prev_watermark": prev,
        }

    merged_parts: list[str] = []
    failed_sources: list[str] = []
    for journal_file in journals:
        source_name = _source_name(journal_file, root)
        try:
            source_text = journal_file.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            if _is_production():
                raise ConsolidationValidationError(f"journal record is malformed: {source_name}") from exc
            failed_sources.append(source_name)
            logger.warning("consolidation skipped malformed journal record: %s", source_name)
            continue
        except OSError as exc:
            if _is_production():
                raise ConsolidationStorageError(f"journal source unavailable: {source_name}") from exc
            failed_sources.append(source_name)
            logger.warning("consolidation skipped unavailable journal source: %s", source_name)
            continue
        merged_parts.append(f"# {journal_file.name}\n\n{source_text}\n")

    if failed_sources:
        if not _is_production():
            return degraded_result("SOURCE_READ_FAILED", failed_sources)
        raise ConsolidationStorageError("journal source read failed")
    if not merged_parts:
        return degraded_result("NO_VALID_RECORDS")

    if target_slug is None:
        target_slug = f"consolidated/{datetime.now(timezone.utc).date().isoformat()}"
    document = "\n\n---\n\n".join(merged_parts)
    try:
        from personal_wiki.vault import upsert_note  # type: ignore
    except (ImportError, ModuleNotFoundError):
        upsert_note = None  # type: ignore[assignment]

    try:
        if upsert_note is not None:
            note_path = upsert_note(
                target_slug,
                document,
                frontmatter={"consolidated_at": datetime.now(timezone.utc).isoformat(), "source": "journal"},
                vault_root=root,
            )
        else:
            slug = target_slug.strip().lstrip("/")
            if not slug.endswith(".md"):
                slug += ".md"
            note_path = notes_dir / slug
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(document, encoding="utf-8")
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        if _is_production():
            raise ConsolidationStorageError("consolidated note write failed") from exc
        logger.warning("consolidated note write degraded: %s", type(exc).__name__)
        return degraded_result("NOTE_WRITE_FAILED")
    if note_path is None:
        if _is_production():
            raise ConsolidationStorageError("consolidated note write returned no path")
        return degraded_result("NOTE_WRITE_FAILED")

    wm_val = datetime.now(timezone.utc).isoformat()
    try:
        write_watermark(wm_val, root)
    except ConsolidationStorageError:
        if _is_production():
            raise
        logger.warning("consolidation note written but watermark update degraded")
        return degraded_result("WATERMARK_WRITE_FAILED", note_path=note_path)
    return {
        "merged": True,
        "degraded": False,
        "watermark": wm_val,
        "note_path": str(note_path),
        "note_count": len(merged_parts),
        "failed_count": 0,
        "failed_sources": [],
        "prev_watermark": prev,
    }
