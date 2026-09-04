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
import hashlib
import secrets
from datetime import datetime, timezone, date as date_type
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator

# Isolated wiki JWT loader — avoids bare `auth` collision (security/auth.py vs admin-console/backend/auth.py)
import importlib.util as _ilu
import sys as _sys2
import pathlib as _pl

def _load_wiki_auth():
    """Load packages/personal-wiki/personal_wiki/auth.py via file location without bare `auth`."""
    # resolve repo root from this file: .../packages/personal-wiki/personal_wiki/vault.py -> parents[2] is repo-ish but robust search
    cand = _pl.Path(__file__).resolve()
    # search upward for packages/personal-wiki/personal_wiki/auth.py
    for p in [cand] + list(cand.parents):
        q = p / "packages" / "personal-wiki" / "personal_wiki" / "auth.py"
        if q.exists():
            ap = q
            break
        q2 = p / "personal_wiki" / "auth.py"
        if q2.exists():
            ap = q2
            break
    else:
        ap = _pl.Path(__file__).parent / "auth.py"
    try:
        if "personal_wiki.auth" in _sys2.modules:
            return _sys2.modules["personal_wiki.auth"]
        spec = _ilu.spec_from_file_location("personal_wiki.auth", str(ap))
        if spec and spec.loader:
            # ensure parent package stub exists without polluting `auth`
            if "personal_wiki" not in _sys2.modules or not hasattr(_sys2.modules["personal_wiki"], "__path__"):
                import types as _types, importlib.machinery as _mach
                pkg = _types.ModuleType("personal_wiki")
                pkg.__path__ = [str(ap.parent)]  # type: ignore
                pkg.__spec__ = _mach.ModuleSpec("personal_wiki", None, is_package=True)  # type: ignore
                _sys2.modules["personal_wiki"] = pkg
            mod = _ilu.module_from_spec(spec)
            _sys2.modules[spec.name] = mod
            spec.loader.exec_module(mod)  # type: ignore
            return mod
    except Exception:
        return None
    return None

_wiki_auth = _load_wiki_auth()
if _wiki_auth is not None:
    verify_wiki_jwt = getattr(_wiki_auth, "verify_wiki_jwt", None)
    verify_tenant_agent_binding = getattr(_wiki_auth, "verify_tenant_agent_binding", None)
    assert_vault_path_safe = getattr(_wiki_auth, "assert_vault_path_safe", None)
    safe_join_vault = getattr(_wiki_auth, "safe_join_vault", None)
    _is_production = getattr(_wiki_auth, "_is_production", lambda: os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod"))
else:
    verify_wiki_jwt = None  # type: ignore
    verify_tenant_agent_binding = None  # type: ignore
    assert_vault_path_safe = None  # type: ignore
    safe_join_vault = None  # type: ignore
    def _is_production() -> bool:
        return os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod")

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
    """Return resolved vault path for given segments (e.g. notes/foo.md) — H3 traversal guard."""
    root = Path(vault_root) if vault_root else get_vault_root()
    if safe_join_vault is not None:
        return safe_join_vault(root, *parts)
    for p in parts:
        if ".." in Path(p).parts or Path(p).is_absolute():
            from fastapi import HTTPException as _HTTPException
            raise _HTTPException(status_code=403, detail=f"PATH_TRAVERSAL: '..' in {p}")
    joined = root.joinpath(*parts)
    if assert_vault_path_safe is not None:
        assert_vault_path_safe(joined, root)
    return joined

def vault_path_for_tenant_agent(tenant_id: str, agent_id: str, *suffix: str, vault_root: Path | str | None = None) -> Path:
    """H3 helper: vault path scoped to tenant/agent with traversal and binding checks."""
    if not tenant_id or not agent_id:
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=401, detail="missing tenant_id or agent_id")
    for val in (tenant_id, agent_id):
        if ".." in val or "/" in val or "\\" in val:
            from fastapi import HTTPException as _HTTPException
            raise _HTTPException(status_code=403, detail=f"PATH_TRAVERSAL: '..' in {val}")
    base = Path(vault_root) if vault_root else get_vault_root()
    if safe_join_vault is not None:
        return safe_join_vault(base, tenant_id, agent_id, *suffix)
    for p in (tenant_id, agent_id, *suffix):
        if ".." in Path(p).parts or Path(p).is_absolute():
            from fastapi import HTTPException as _HTTPException
            raise _HTTPException(status_code=403, detail=f"PATH_TRAVERSAL: '..' in {p}")
    joined = base.joinpath(tenant_id, agent_id, *suffix)
    if assert_vault_path_safe is not None:
        assert_vault_path_safe(joined, base)
    return joined


def ensure_vault_dirs(vault_root: Path | str | None = None) -> Path:
    """Ensure vault subdirs exist. Returns vault root."""
    root = Path(vault_root) if vault_root else get_vault_root()
    for d in (root, root / "journal", root / "notes", root / "attachments"):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    return root


# ---------------------------------------------------------------------------
# Attachment streaming store (owner-scoped, bounded, atomic)
# ---------------------------------------------------------------------------

ATTACHMENT_MAX_BYTES = 500 * 1024 * 1024  # 524288000 — full upload/storage bound (NOT LLM transfer)
ATTACHMENT_STREAM_CHUNK = 65536  # 64KB streaming unit — never hold full 500MB in memory
ATTACHMENT_SUBDIR = "attachments"


class AttachmentTooLargeError(ValueError):
    """Raised when streamed attachment exceeds max_bytes; partial output is removed."""


def sanitize_attachment_filename(name: str | None) -> str:
    """Sanitize to a single safe path segment (no traversal, bounded length)."""
    base = Path(name or "attachment").name
    # strip NUL/control
    base = re.sub(r"[\x00-\x1f\x7f]", "_", base)
    base = base.strip().strip(".") or "attachment"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "attachment"
    safe = safe.replace("..", "_")
    safe = re.sub(r"_+", "_", safe)
    return safe[:180] or "attachment"


def _validate_owner_id(value: str, label: str) -> str:
    v = (value or "").strip()
    if not v:
        raise ValueError(f"missing {label}")
    # traversal defense: owner ids are single path segments (colons allowed, e.g. agent:assistant:x)
    if "/" in v or "\\" in v or ".." in v or Path(v).is_absolute():
        raise ValueError(f"PATH_TRAVERSAL in {label}")
    for seg in Path(v).parts:
        if seg == "..":
            raise ValueError(f"PATH_TRAVERSAL in {label}")
    if "\x00" in v:
        raise ValueError(f"invalid NUL in {label}")
    return v


def _iter_stream_chunks(stream: Any, chunk_size: int = ATTACHMENT_STREAM_CHUNK) -> Iterator[bytes]:
    """Normalize bytes / file-like (.read) / iterable-of-bytes to a byte-chunk iterator."""
    if stream is None:
        return
        yield  # make this a generator
    if isinstance(stream, (bytes, bytearray)):
        mv = bytes(stream)
        for i in range(0, len(mv), chunk_size):
            yield mv[i:i + chunk_size]
        return
    read = getattr(stream, "read", None)
    if callable(read):
        while True:
            # Read errors must propagate: store_attachment deletes the
            # .part partial and re-raises (never stored=True truncated).
            chunk = read(chunk_size)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            yield bytes(chunk)
        return
    try:
        for _chunk in stream:  # type: ignore[union-attr]
            if _chunk is None:
                continue
            if isinstance(_chunk, str):
                _b = _chunk.encode("utf-8", errors="replace")
            elif isinstance(_chunk, (bytes, bytearray, memoryview)):
                _b = bytes(_chunk)
            else:
                continue
            if _b:
                # re-split oversized yielded chunks so memory stays bounded
                for i in range(0, len(_b), chunk_size):
                    yield _b[i:i + chunk_size]
    except TypeError:
        raise ValueError("stream must be bytes, a file-like with .read(), or an iterable of bytes")


def store_attachment(
    tenant_id: str,
    agent_id: str,
    filename: str,
    stream: bytes | BinaryIO | Iterable[bytes],
    *,
    file_id: str | None = None,
    vault_root: Path | str | None = None,
    max_bytes: int = ATTACHMENT_MAX_BYTES,
) -> dict[str, Any]:
    """Stream an attachment to the owner-scoped vault without holding it in memory.

    Layout: <vault>/<tenant>/<agent>/attachments/[<file_id>/]<sanitized-filename>
    - Incremental sha256 while writing in 64KB units; cap enforced during stream.
    - Atomic ``.part`` -> :func:`os.replace`; mode ``0600`` on the final file.
    - Traversal defense on tenant/agent/filename/file_id; cap exceed deletes partial.
    - Returns metadata dict with owner-scoped *relative* ``vault_path`` (POSIX),
      ``stored=True``, ``size`` (actual bytes), ``sha256``, ``filename``.

    Existing vault APIs are unchanged.
    """
    tenant = _validate_owner_id(tenant_id, "tenant_id")
    agent = _validate_owner_id(agent_id, "agent_id")
    safe_name = sanitize_attachment_filename(filename)
    try:
        limit = int(max_bytes)
    except Exception:
        limit = ATTACHMENT_MAX_BYTES
    if limit <= 0:
        limit = ATTACHMENT_MAX_BYTES
    # Hard cap: caller-supplied max_bytes can only lower the bound, never raise it.
    if limit > ATTACHMENT_MAX_BYTES:
        limit = ATTACHMENT_MAX_BYTES

    root = Path(vault_root) if vault_root else get_vault_root()
    owner_root = vault_path_for_tenant_agent(tenant, agent, vault_root=root)
    dest_dir = owner_root / ATTACHMENT_SUBDIR
    # file_id subdir isolates re-uploads; only safe alnum ids are used as a segment.
    # Unsafe-but-present ids map to a deterministic hash segment (no silent
    # collide onto the bare owner dir, which would overwrite across files).
    safe_fid = ""
    if file_id:
        fid = str(file_id).strip()
        if re.match(r"^[A-Za-z0-9_-]{1,128}$", fid):
            safe_fid = fid
        elif fid:
            safe_fid = "file-" + hashlib.sha256(fid.encode("utf-8", errors="replace")).hexdigest()[:16]
    if safe_fid:
        dest_dir = dest_dir / safe_fid
    # Pre-mkdir fail-closed: owner must resolve inside the vault root and
    # neither the owner root nor dest may be a symlink (no FS side-effect
    # outside the vault when a link is pre-planted). Hard 500MiB cap is
    # enforced at write time regardless of caller max_bytes (see limit).
    try:
        _root_pre = root.resolve()
        _owner_pre = owner_root.resolve()
        if _owner_pre != _root_pre and _root_pre not in _owner_pre.parents:
            raise ValueError("vault owner escapes vault root")
        if os.path.islink(owner_root) or os.path.islink(dest_dir):
            raise ValueError("symlinked vault destination")
        _dir_pre = dest_dir.resolve()
        if _dir_pre != _owner_pre and _owner_pre not in _dir_pre.parents:
            raise ValueError("vault destination escapes owner root")
        if _dir_pre != _root_pre and _root_pre not in _dir_pre.parents:
            raise ValueError("vault destination escapes vault root")
    except ValueError:
        raise
    except Exception:
        raise ValueError("vault destination validation failed")
    # Owner-only dirs (0700): attachments may hold secrets (e.g. *.json keys).
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(dest_dir, 0o700)
    except Exception:
        pass
    # Symlink/traversal containment: the resolved dir must stay inside the
    # resolved owner root (refuses pre-planted symlink escapes). Fail closed.
    try:
        _owner_resolved = owner_root.resolve()
        _dir_resolved = dest_dir.resolve()
        if _dir_resolved != _owner_resolved and _owner_resolved not in _dir_resolved.parents:
            raise ValueError("vault destination escapes owner root")
    except ValueError:
        raise
    except Exception:
        raise ValueError("vault destination validation failed")
    dest = dest_dir / safe_name

    tmp = dest_dir / f"{safe_name}.part-{os.getpid()}-{secrets.token_hex(6)}"
    digest = hashlib.sha256()
    total = 0
    # Secure temp creation: mode 0600 from birth (no world-readable window)
    # + O_EXCL to refuse symlink/hardlink races on the random suffix
    # + O_NOFOLLOW so a planted symlink at the random name fails closed.
    _open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        _tmp_fd = os.open(str(tmp), _open_flags, 0o600)
    except FileExistsError:
        raise ValueError("attachment temp collision; retry")
    except OSError as _oe:
        # ELOOP with O_NOFOLLOW (or EEXIST): symlink race — fail closed.
        import errno as _errno
        if getattr(_oe, "errno", None) in (_errno.ELOOP, _errno.EEXIST):
            raise ValueError("attachment temp collision; retry")
        raise
    try:
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        try:
            _fh = os.fdopen(_tmp_fd, "wb")
        except BaseException:
            try:
                os.close(_tmp_fd)
            except Exception:
                pass
            raise
        with _fh:
            for chunk in _iter_stream_chunks(stream):
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    raise AttachmentTooLargeError(
                        f"attachment exceeds {limit} bytes cap"
                    )
                digest.update(chunk)
                _fh.write(chunk)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise
    # 0600 effective before the atomic publish so the dest never appears 0644.
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    # Re-validate containment just before publish (closes mkdir-check vs
    # replace TOCTOU: a swapped symlink at dest_dir fails closed here).
    try:
        _owner_re = owner_root.resolve()
        _dir_re = dest_dir.resolve()
        if _dir_re != _owner_re and _owner_re not in _dir_re.parents:
            raise ValueError("vault destination escapes owner root")
    except ValueError:
        try:
            if tmp.exists() or os.path.lexists(str(tmp)):
                tmp.unlink()
        except Exception:
            pass
        raise
    except Exception:
        try:
            if tmp.exists() or os.path.lexists(str(tmp)):
                tmp.unlink()
        except Exception:
            pass
        raise ValueError("vault destination validation failed")
    try:
        os.replace(tmp, dest)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    try:
        os.chmod(dest, 0o600)
    except Exception:
        pass
    # Post-publish containment: dest must resolve inside the vault root.
    # Fail closed — remove an escaped dest instead of returning a logical path.
    try:
        rel = dest.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        try:
            try:
                if dest.exists() or os.path.lexists(str(dest)):
                    dest.unlink()
            except Exception:
                pass
        finally:
            pass
        raise ValueError("vault destination escapes vault root")
    return {
        "stored": True,
        "vault_path": rel,
        "filename": safe_name,
        "size": total,
        "sha256": digest.hexdigest(),
        "tenant_id": tenant,
        "agent_id": agent,
    }


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
    wiki_jwt: str | None = None,
    tenant_id: str | None = None,
    agent_id: str | None = None,
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
        # H3: verified JWT owner isolation (no unverified claims)
        if wiki_jwt is not None and verify_wiki_jwt is not None:
            payload = verify_wiki_jwt(wiki_jwt, required_scope="wiki:write")
            if tenant_id or agent_id:
                if verify_tenant_agent_binding is not None:
                    verify_tenant_agent_binding(payload, tenant_id, agent_id)
            if tenant_id is None:
                tenant_id = payload.get("tenant_id")
            if agent_id is None:
                agent_id = payload.get("agent_id")
        elif _is_production() and wiki_jwt is None and (tenant_id or agent_id):
            from fastapi import HTTPException as _HTTPException
            raise _HTTPException(status_code=401, detail="wiki JWT required in production")
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
    wiki_jwt: str | None = None,
    tenant_id: str | None = None,
    agent_id: str | None = None,
) -> Path | None:
    """Create or overwrite a note at notes/<slug>.md — H3 verified JWT + traversal guard.

    slug may include subdirs e.g. "project/spec".
    Returns Path or None on failure.
    """
    try:
        # H3: reject path traversal in slug
        raw_slug = slug.strip().lstrip("/")
        for seg in raw_slug.split("/"):
            if seg == "..":
                from fastapi import HTTPException as _HTTPException
                raise _HTTPException(status_code=403, detail="PATH_TRAVERSAL: '..' in slug")
        # H3: verified JWT
        if wiki_jwt is not None and verify_wiki_jwt is not None:
            payload = verify_wiki_jwt(wiki_jwt, required_scope="wiki:write")
            if tenant_id or agent_id:
                if verify_tenant_agent_binding is not None:
                    verify_tenant_agent_binding(payload, tenant_id, agent_id)
            if tenant_id is None:
                tenant_id = payload.get("tenant_id")
            if agent_id is None:
                agent_id = payload.get("agent_id")
        elif _is_production() and wiki_jwt is None and (tenant_id or agent_id):
            from fastapi import HTTPException as _HTTPException
            raise _HTTPException(status_code=401, detail="wiki JWT required in production")
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
        # H3: ensure final path is within notes dir (traversal guard)
        if assert_vault_path_safe is not None:
            try:
                assert_vault_path_safe(nfile.resolve() if nfile.exists() else nfile.absolute(), get_notes_dir(root).resolve())
            except Exception:
                raise
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
