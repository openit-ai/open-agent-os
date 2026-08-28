"""Vault paths, journal append with frontmatter, notes upsert.

- All file ops are best-effort and create dirs lazily.
- No hard deps; pure stdlib + pathlib.
- Env:
    OAOS_WIKI_VAULT or PERSONAL_WIKI_VAULT or VAULT_ROOT -> vault root
    OAOS_WIKI_JOURNAL_MAX_CHARS -> truncation limit (default 4000)
"""
from __future__ import annotations

import os
import re
import json
from datetime import datetime, timezone, date as date_type
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Vault paths
# ---------------------------------------------------------------------------

_ENV_KEYS = ("OAOS_WIKI_VAULT", "PERSONAL_WIKI_VAULT", "VAULT_ROOT", "PERSONAL_WIKI_ROOT")
_DEFAULT_SUB = Path.home() / ".open-agent-os" / "wiki-vault"


def get_vault_root() -> Path:
    """Resolve vault root from env or default. Always returns Path."""
    for k in _ENV_KEYS:
        v = os.getenv(k)
        if v and v.strip():
            return Path(v.strip()).expanduser().resolve()
    # fallback: repo-relative data/vault if exists? else home
    # Prefer explicit home default to avoid polluting repo
    return _DEFAULT_SUB


def get_journal_dir(vault_root: Path | str | None = None) -> Path:
    return (Path(vault_root) if vault_root else get_vault_root()) / "journal"


def get_notes_dir(vault_root: Path | str | None = None) -> Path:
    return (Path(vault_root) if vault_root else get_vault_root()) / "notes"


def get_attachments_dir(vault_root: Path | str | None = None) -> Path:
    return (Path(vault_root) if vault_root else get_vault_root()) / "attachments"


def vault_path(*parts: str, vault_root: Path | str | None = None) -> Path:
    """Join parts under vault root."""
    root = Path(vault_root) if vault_root else get_vault_root()
    return root.joinpath(*parts)


def ensure_vault_dirs(vault_root: Path | str | None = None) -> Path:
    """Ensure vault subdirs exist. Returns vault root."""
    root = Path(vault_root) if vault_root else get_vault_root()
    for d in (root, root / "journal", root / "notes", root / "attachments"):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    return root


def journal_file_for_date(
    d: str | date_type | datetime | None = None,
    vault_root: Path | str | None = None,
) -> Path:
    """Journal file for a given date. Default today UTC, format journal/YYYY-MM-DD.md"""
    if d is None:
        ds = datetime.now(timezone.utc).date().isoformat()
    elif isinstance(d, str):
        # accept YYYY-MM-DD or iso
        try:
            ds = d[:10]
            # validate
            datetime.fromisoformat(ds)
        except Exception:
            ds = datetime.now(timezone.utc).date().isoformat()
    elif isinstance(d, datetime):
        ds = d.date().isoformat()
    else:  # date
        ds = d.isoformat()
    # support nested YYYY/MM/DD if env asks, but default flat
    return get_journal_dir(vault_root) / f"{ds}.md"


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

def _yaml_escape(v: Any) -> str:
    s = str(v)
    # simple escaping for yaml frontmatter
    if any(c in s for c in (":", "#", '"', "'", "\n", "[", "]", "{", "}")):
        return json.dumps(s)  # quoted
    return s


def _frontmatter(data: dict[str, Any]) -> str:
    lines = ["---"]
    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, (dict, list)):
            # use json for complex
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        else:
            lines.append(f"{k}: {_yaml_escape(v)}")
    lines.append("---")
    return "\n".join(lines)


def _sanitize_slug(s: str) -> str:
    s = s.strip()
    s = re.sub(r"[^\w\-./ ]", "-", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-") or "untitled"


# ---------------------------------------------------------------------------
# Journal append with frontmatter
# ---------------------------------------------------------------------------

def append_journal(
    trace_id: str,
    tool_name: str,
    result: Any,
    max_chars: int | None = None,
    extra: dict[str, Any] | None = None,
    vault_root: Path | str | None = None,
    when: datetime | None = None,
) -> Path | None:
    """Append a journal entry with YAML frontmatter.

    Creates/append to journal/YYYY-MM-DD.md with:
        ---
        trace_id: ...
        tool: ...
        date: ...
        ---
        truncated result

    Returns Path of journal file or None on failure (best-effort).
    Truncation defaults to OAOS_WIKI_JOURNAL_MAX_CHARS env or 4000.
    """
    try:
        if max_chars is None:
            try:
                max_chars = int(os.getenv("OAOS_WIKI_JOURNAL_MAX_CHARS", "4000"))
            except Exception:
                max_chars = 4000

        when = when or datetime.now(timezone.utc)
        root = ensure_vault_dirs(vault_root)
        jfile = journal_file_for_date(when.date(), root)

        # Prepare truncated result string
        if result is None:
            rtext = ""
        elif isinstance(result, str):
            rtext = result
        else:
            try:
                rtext = json.dumps(result, ensure_ascii=False, default=str, indent=2)
            except Exception:
                rtext = str(result)

        if len(rtext) > max_chars:
            rtext = rtext[:max_chars] + f"\n...[truncated {len(rtext)-max_chars} chars]"

        fm = {
            "trace_id": trace_id or "unknown",
            "tool": tool_name or "unknown",
            "date": when.isoformat(),
            "type": "tool_result",
        }
        if extra:
            fm.update({k: v for k, v in extra.items() if k not in fm})

        block = _frontmatter(fm) + "\n\n" + rtext + "\n\n"
        # append with separator
        jfile.parent.mkdir(parents=True, exist_ok=True)
        with jfile.open("a", encoding="utf-8") as f:
            # add horizontal rule if file already had content
            if jfile.stat().st_size > len(block):
                # file existed before this write — ensure separator
                f.write("\n---\n\n")
            f.write(block)
        # optional pgvector embed (env OAOS_EMBED_ENABLED)
        try:
            if os.getenv("OAOS_EMBED_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on"):
                _maybe_embed(jfile, metadata={"trace_id": trace_id, "tool": tool_name, "kind": "journal"})
        except Exception:
            pass
        return jfile
    except Exception:
        # best-effort: never raise
        return None


def _maybe_embed(path: Path, metadata: dict[str, Any] | None = None) -> None:
    """Best-effort embed vault file via personal_wiki.embed (lazy, never raises)."""
    try:
        # lazy import to avoid hard dep
        import importlib.util

        found = importlib.util.find_spec("personal_wiki.embed")
        if found is None:
            # try adding package to path fallback
            return
        from personal_wiki.embed import embed_file_sync  # type: ignore

        # Extract owner/tenant from path or metadata if available
        owner = (metadata or {}).get("owner") or (metadata or {}).get("user_id") or "employee:anonymous"
        tenant_id = (metadata or {}).get("tenant_id") or "default"
        agent_id = (metadata or {}).get("agent_id")
        embed_file_sync(path, metadata=metadata, owner=owner, tenant_id=tenant_id, agent_id=agent_id)
    except Exception:
        pass


# Backwards-compat aliases
journal_append = append_journal
append_to_journal = append_journal


# ---------------------------------------------------------------------------
# Notes upsert
# ---------------------------------------------------------------------------

def upsert_note(
    slug: str,
    content: str,
    frontmatter: dict[str, Any] | None = None,
    vault_root: Path | str | None = None,
) -> Path | None:
    """Create or overwrite a note at notes/<slug>.md with optional frontmatter.

    slug may include subdirs e.g. "project/spec".
    Returns Path or None on failure.
    """
    try:
        root = ensure_vault_dirs(vault_root)
        # normalize slug
        slug = slug.strip().lstrip("/")
        if not slug:
            slug = "untitled"
        if not slug.endswith(".md"):
            slug = slug + ".md"
        # sanitize each part but preserve /
        parts = slug.split("/")
        safe_parts = [_sanitize_slug(p) if i < len(parts)-1 else p for i, p in enumerate(parts)]
        # last part keep .md
        nfile = get_notes_dir(root).joinpath(*safe_parts)
        nfile.parent.mkdir(parents=True, exist_ok=True)

        fm = ""
        if frontmatter:
            fm = _frontmatter(frontmatter) + "\n\n"
        nfile.write_text(fm + content, encoding="utf-8")
        # optional pgvector embed (env OAOS_EMBED_ENABLED)
        try:
            if os.getenv("OAOS_EMBED_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on"):
                _maybe_embed(nfile, metadata={"slug": slug, "kind": "note", **(frontmatter or {})})
        except Exception:
            pass
        return nfile
    except Exception:
        return None


# Alias
note_upsert = upsert_note
