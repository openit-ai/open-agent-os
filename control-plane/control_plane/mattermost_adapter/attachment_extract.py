"""CP-side bounded extraction for Mattermost attachment refs (owner-safe Vault read path).

The bridge (scripts/oaos-mm-bridge.py) streams attachment bytes into the
owner-scoped Vault and forwards only metadata refs (relative ``vault_path``,
``stored``/``sha256``/``size``). It never extracts. This module runs just
before ``session_store.append_prompt`` / ACP in the webhook handler:

- validates each ref against the verified ``tenant_id`` / ``mapping.agent_principal``
- resolves only the canonical ``OAOS_WIKI_VAULT`` root via
  ``safe_join`` / ``assert_vault_path_safe`` (traversal-safe)
- refuses absolute paths, scheme URLs (``file://``, ``://``), ``..`` segments,
  and cross-owner refs (must live under ``<tenant>/<agent>/``)
- skips sensitive refs (``reason == "sensitive"`` or sensitive filename) —
  bytes stay preserved in the owner vault, nothing reaches the LLM
- for extractable formats only, runs the existing
  ``packages/personal-wiki/personal_wiki/extractor.py`` in
  ``asyncio.to_thread`` with a bounded char limit and a conservative
  max-extraction file size (never the 500MB store cap)
- applies defense-in-depth secret masking (key=value and JSON secret fields)
- emits only bounded ``extracted_text`` plus safe relative citation metadata;
  never raw bytes, absolute paths, Mattermost tokens/URLs, or user paths

Image refs (``kind == "image"`` / ``image/*``) are preserved untouched for the
existing ACP image gate — except ``url``/``local_path`` leak fields, which are
stripped (the gate only needs ``data_url``/``base64``). Non-image refs never
become ``image_url`` (existing ACP gate, unchanged).

All failures are fail-closed per ref (metadata-only, ``extracted_text=None``)
and the helper itself never raises.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

# Bounded LLM-facing text per attachment (matches ACP citation bound).
MAX_EXTRACTED_CHARS = 20000
# Conservative extraction input cap — the 500MB value is the durable store
# bound, never an extraction/LLM bound. Files above this are metadata-only.
MAX_EXTRACT_BYTES = 10 * 1024 * 1024


def _max_chars() -> int:
    try:
        return max(1, min(20000, int(os.getenv("OAOS_CP_EXTRACT_MAX_CHARS", str(MAX_EXTRACTED_CHARS)))))
    except ValueError:
        return MAX_EXTRACTED_CHARS


def _max_bytes() -> int:
    try:
        return max(1024, min(MAX_EXTRACT_BYTES, int(os.getenv("OAOS_CP_EXTRACT_MAX_BYTES", str(MAX_EXTRACT_BYTES)))))
    except ValueError:
        return MAX_EXTRACT_BYTES


# Formats the bundled extractor can handle as text. Anything else
# (archives, audio/video, hwp/hwpx, binaries without text decoding, …)
# stays metadata-only. Images are preserved via the image path, not extracted.
_EXTRACTABLE_EXTS = frozenset({
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".txt", ".md", ".markdown", ".csv", ".tsv",
    ".json", ".jsonl", ".xml", ".yaml", ".yml", ".log",
})

_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".tiff", ".tif", ".svg", ".heic", ".heif",
})

# Defense-in-depth secret masking (same shapes as bridge / ACP gate).
_SENSITIVE_KEY_RE = re.compile(
    r"client_secret|private[\s_\-]*key|refresh[\s_\-]*token|access[\s_\-]*token|api[\s_\-]*key|passw(?:or)?d",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(client_secret|private[\s_\-]*key|refresh[\s_\-]*token|access[\s_\-]*token|api[\s_\-]*key|passw(?:or)?d)\s*([:=]|=>)\s*\S+",
    re.IGNORECASE,
)
_SECRET_JSON_VALUE_RE = re.compile(
    r'("(?:client_secret|private[\s_\-]*key|refresh[\s_\-]*token|access[\s_\-]*token|api[\s_\-]*key|passw(?:or)?d)"\s*:\s*")[^"]*(")',
    re.IGNORECASE,
)


def mask_secrets(text: str) -> str:
    """Mask secret values in key=value and JSON-quoted shapes."""
    try:
        masked = _SECRET_JSON_VALUE_RE.sub(lambda m: f"{m.group(1)}***{m.group(2)}", text or "")
        return _SECRET_VALUE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", masked)
    except Exception:
        return text or ""


def _is_sensitive_ref(ref: dict) -> bool:
    """True when a ref must never be extracted or previewed."""
    try:
        if str(ref.get("reason") or "").strip().lower() == "sensitive":
            return True
        name = str(ref.get("filename") or "")
        if name and _SENSITIVE_KEY_RE.search(name):
            return True
    except Exception:
        pass
    return False


def _is_image_ref(ref: dict) -> bool:
    try:
        kind = str(ref.get("kind") or "").strip().lower()
        if kind == "image":
            return True
        if kind in ("text_preview", "stored_only", "stored", "text", "document", "file"):
            return False
        mime = str(ref.get("mime_type") or ref.get("mimeType") or ref.get("mime") or "")
        mime = mime.strip().lower().split(";")[0].strip()
        if mime:
            return mime.startswith("image/")
        ext = Path(str(ref.get("filename") or "")).suffix.lower()
        if ext:
            return ext in _IMAGE_EXTS
    except Exception:
        pass
    return False


def _safe_basename(name: Any) -> str:
    try:
        base = Path(str(name or "attachment")).name
    except Exception:
        base = "attachment"
    base = re.sub(r"[\x00-\x1f\x7f]", "_", base).strip().strip(".") or "attachment"
    return base[:180]


# ---------------------------------------------------------------------------
# Lazy loaders (never fail at import; fall back to stdlib equivalents)
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _ensure_wiki_on_path() -> None:
    try:
        pkg = _repo_root() / "packages" / "personal-wiki"
        if pkg.is_dir() and str(pkg) not in sys.path:
            sys.path.insert(0, str(pkg))
    except Exception:
        pass


def _load_vault_helpers() -> tuple[Any, Any, Any]:
    """Return (get_vault_root, safe_join_vault, assert_vault_path_safe) or Nones."""
    try:
        _ensure_wiki_on_path()
        from personal_wiki.vault import (  # type: ignore
            assert_vault_path_safe as _assert,
            get_vault_root as _root,
            safe_join_vault as _join,
        )
        return _root, _join, _assert
    except Exception:
        pass
    try:  # file-location fallback (isolated, no package import)
        import importlib.util as _ilu

        target = _repo_root() / "packages" / "personal-wiki" / "personal_wiki" / "vault.py"
        if target.exists():
            spec = _ilu.spec_from_file_location("oaos_cp_vault_fallback", str(target))
            if spec and spec.loader:
                mod = _ilu.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore
                return (
                    getattr(mod, "get_vault_root", None),
                    getattr(mod, "safe_join_vault", None),
                    getattr(mod, "assert_vault_path_safe", None),
                )
    except Exception:
        pass
    return None, None, None


def _load_extractor():
    """Return extractor.extract_text callable or None (lazy deps stay lazy)."""
    try:
        _ensure_wiki_on_path()
        from personal_wiki.extractor import extract_text  # type: ignore

        return extract_text
    except Exception:
        pass
    try:
        import importlib.util as _ilu

        target = _repo_root() / "packages" / "personal-wiki" / "personal_wiki" / "extractor.py"
        if target.exists():
            spec = _ilu.spec_from_file_location("oaos_cp_extractor_fallback", str(target))
            if spec and spec.loader:
                mod = _ilu.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore
                fn = getattr(mod, "extract_text", None)
                if callable(fn):
                    return fn
    except Exception:
        pass
    return None


def _canonical_root(vault_root: Path | str | None) -> Path:
    if vault_root is not None:
        return Path(vault_root).expanduser()
    get_root, _, _ = _load_vault_helpers()
    if callable(get_root):
        try:
            return Path(str(get_root()))
        except Exception:
            pass
    for key in ("OAOS_WIKI_VAULT", "PERSONAL_WIKI_VAULT", "VAULT_ROOT"):
        val = os.getenv(key, "").strip()
        if val:
            return Path(val).expanduser()
    return Path.home() / ".open-agent-os" / "wiki-vault"


def _resolve_owner_file(
    vault_path: str, tenant_id: str, agent_principal: str, root: Path
) -> Path | None:
    """Validate ownership + resolve under the canonical root. None = refuse."""
    try:
        vp = (vault_path or "").strip()
        if not vp:
            return None
        # Normalize first so backslash traversal can't dodge the ".." check.
        norm = vp.replace("\\", "/")
        # Refuse absolute paths, drive-letter paths, file:// URLs, any scheme, and traversal.
        if norm.startswith("/") or norm.startswith("file://") or "://" in norm:
            return None
        if re.match(r"^[A-Za-z]:/", norm):
            return None
        if Path(vp).is_absolute() or Path(norm).is_absolute():
            return None
        if ".." in Path(norm).parts or ".." in norm.split("/"):
            return None
        # Owner scope: <tenant>/<agent>/… (single-segment prefix match).
        tenant = (tenant_id or "").strip()
        agent = (agent_principal or "").strip()
        if not tenant or not agent:
            return None
        if "/" in tenant or "\\" in tenant or ".." in tenant:
            return None
        if "/" in agent or "\\" in agent or ".." in agent:
            return None
        if not (norm == f"{tenant}/{agent}" or norm.startswith(f"{tenant}/{agent}/")):
            return None
        _, safe_join, assert_safe = _load_vault_helpers()
        if callable(safe_join):
            joined = Path(str(safe_join(root, norm)))
        else:  # stdlib fallback mirror of safe_join_vault
            joined = root.joinpath(*norm.split("/"))
        if callable(assert_safe):
            assert_safe(joined, root)
        else:
            try:
                joined.resolve().relative_to(root.resolve())
            except ValueError:
                return None
        # Defense in depth: resolved file must also sit under the owner dir.
        try:
            owner_dir = root.joinpath(tenant, agent).resolve()
            if joined.resolve() != owner_dir and owner_dir not in joined.resolve().parents:
                # allow non-existent targets: compare absolute normalized paths
                if str(joined.resolve()) != str(owner_dir) and not str(joined.resolve()).startswith(str(owner_dir) + os.sep):
                    return None
        except Exception:
            return None
        return joined
    except Exception:
        return None


def _looks_owner_scoped(vp: Any, tenant_id: str, agent_principal: str) -> bool:
    """Syntactic owner-scope check (no fs access). False = refuse/downgrade.

    Mirrors the path-shape rules of ``_resolve_owner_file`` so sanitizers never
    keep a traversal / absolute / URL / cross-owner string as a citation.
    """
    try:
        s = (vp or "").strip() if isinstance(vp, str) else str(vp or "").strip()
        if not s:
            return False
        norm = s.replace("\\", "/")
        if norm.startswith("/") or norm.startswith("file://") or "://" in norm:
            return False
        if re.match(r"^[A-Za-z]:/", norm):
            return False
        if Path(s).is_absolute() or Path(norm).is_absolute():
            return False
        if ".." in Path(norm).parts or ".." in norm.split("/"):
            return False
        tenant = (tenant_id or "").strip()
        agent = (agent_principal or "").strip()
        if not tenant or not agent:
            return False
        if "/" in tenant or "\\" in tenant or ".." in tenant:
            return False
        if "/" in agent or "\\" in agent or ".." in agent:
            return False
        return norm == f"{tenant}/{agent}" or norm.startswith(f"{tenant}/{agent}/")
    except Exception:
        return False


def _redacted_name(filename: Any) -> str:
    """Citation filename that never carries a secret word toward the LLM."""
    try:
        ext = Path(_safe_basename(filename)).suffix.lower()
        ext = re.sub(r"[^a-z0-9.]", "", ext)[:16]
    except Exception:
        ext = ""
    return f"redacted-attachment{ext}"


_SANITIZED_KEYS = (
    "file_id", "attachment_id", "kind", "filename", "mime_type",
    "size", "source", "vault_path", "stored", "sha256",
    "extractable", "extract_hint", "reason", "extracted_text",
)


def _sanitize_image_ref(ref: dict, tenant_id: str = "", agent_principal: str = "") -> dict:
    """Preserve image refs for the ACP image gate; strip leak fields.

    Bytes (``data_url``/``base64``) are kept only when ``vault_path`` is a
    verified owner-scoped ref — never for invalid/cross-owner refs. Sensitive
    filenames are redacted so the secret word never reaches the LLM.
    """
    out: dict[str, Any] = {}
    try:
        for key in ("file_id", "attachment_id", "kind", "filename", "mime_type", "size",
                    "source", "vault_path", "stored", "sha256", "extractable",
                    "extract_hint", "data_url", "base64"):
            if key in ref and ref[key] is not None:
                out[key] = ref[key]
        out["kind"] = "image"
        verified = _looks_owner_scoped(ref.get("vault_path"), tenant_id, agent_principal)
        if _is_sensitive_ref(ref) or _SENSITIVE_KEY_RE.search(_safe_basename(ref.get("filename"))):
            red = _redacted_name(ref.get("filename"))
            out["filename"] = red
            # always redact: even a verified owner path carries the
            # secret-bearing filename toward the LLM.
            out["vault_path"] = red
        else:
            out["filename"] = _safe_basename(ref.get("filename"))
            vp = str(ref.get("vault_path") or "")
            # citation only: keep verified owner-scoped relative path, never
            # absolute/URL/traversal/cross-owner (empty, not a rewritten path).
            if verified:
                out["vault_path"] = mask_secrets(vp)[:300]
            else:
                out["vault_path"] = ''
        if not verified:
            # never forward bytes for an unverified ref
            out.pop("data_url", None)
            out.pop("base64", None)
        out["extracted_text"] = None
        # never forward: url (Mattermost API), local_path (absolute), preview, token
    except Exception:
        pass
    return out


def _sanitize_meta_ref(ref: dict, extracted_text: str | None, tenant_id: str = "", agent_principal: str = "") -> dict:
    """Metadata-only ref: allowlisted citation fields + bounded extracted_text.

    Sensitive refs are redacted (filename + path) with ``extracted_text=None``.
    Invalid/cross-owner refs keep an empty citation — never rewritten into
    a valid-looking owner path or a misleading bare filename.
    """
    out: dict[str, Any] = {}
    try:
        for key in _SANITIZED_KEYS:
            if key in ref and ref[key] is not None:
                out[key] = ref[key]
        if _is_sensitive_ref(ref) or _SENSITIVE_KEY_RE.search(_safe_basename(ref.get("filename"))):
            red = _redacted_name(ref.get("filename"))
            out["filename"] = red
            out["vault_path"] = red
            out["extracted_text"] = None
            # Sensitive refs are never extractable for the LLM path (bytes
            # stay preserved in the owner vault only).
            out["extractable"] = False
        else:
            out["filename"] = _safe_basename(ref.get("filename"))
            vp = str(ref.get("vault_path") or "")
            if _looks_owner_scoped(vp, tenant_id, agent_principal):
                out["vault_path"] = mask_secrets(vp)[:300]
            else:
                out["vault_path"] =  ''
            if isinstance(ref.get("mime_type"), str):
                out["mime_type"] = ref["mime_type"].strip().lower().split(";")[0].strip() or "unknown"
            out["extracted_text"] = extracted_text
            # Recompute extractability against what the CP extractor can
            # actually handle: the bridge hint covers hwp/hwpx (stored for
            # preservation) but this module cannot extract them — leaving
            # True would advertise unusable extraction.
            try:
                _ext = Path(str(out.get("filename") or "")).suffix.lower()
            except Exception:
                _ext = ""
            out["extractable"] = bool(_ext and _ext in _EXTRACTABLE_EXTS)
        # never forward: preview/base64/data_url/url/local_path/token/raw bytes
        for leak in ("preview", "base64", "data_url", "dataUrl", "url", "local_path",
                     "localPath", "local_file", "file_path", "download_url",
                     "token", "absolute_path", "path", "bytes", "content"):
            out.pop(leak, None)
    except Exception:
        pass
    return out


def _extract_sync(resolved: Path, max_chars: int) -> str | None:
    try:
        fn = _load_extractor()
        if not callable(fn):
            return None
        text = fn(resolved, max_chars)
        if not isinstance(text, str) or not text.strip():
            return None
        # Skip extractor stub/error markers — metadata-only instead.
        # Match known marker prefixes only: legitimate content (JSON arrays,
        # markdown links, …) may start with "[" and must stay usable.
        _stripped = text.strip()
        _stub_prefixes = (
            "[pdf extraction ", "[pdf ",
            "[docx extraction ", "[docx ",
            "[xlsx extraction ", "[xlsx ",
            "[pptx extraction ", "[pptx ",
            "[unsupported extension", "[unsupported ",
            "[extraction failed", "[txt read error",
            "[file not found", "[Image Attachment",
            "[LLM Vision",
        )
        if _stripped.startswith(_stub_prefixes):
            return None
        return mask_secrets(_stripped)[:max_chars] or None
    except Exception:
        return None


async def enrich_attachment_refs(
    attachment_refs: list[dict] | None,
    *,
    tenant_id: str,
    agent_principal: str,
    vault_root: Path | str | None = None,
    max_chars: int | None = None,
    max_extract_bytes: int | None = None,
) -> list[dict]:
    """Owner-validate + bounded-extract each stored ref. Never raises.

    Returns a new list of sanitized refs carrying at most bounded
    ``extracted_text`` plus safe relative citation metadata.
    """
    refs = attachment_refs or []
    if not isinstance(refs, list):
        return []
    # Hard caps: caller-supplied bounds can only lower, never raise.
    try:
        limit_chars = _max_chars() if max_chars is None else max(1, min(MAX_EXTRACTED_CHARS, int(max_chars)))
    except Exception:
        limit_chars = _max_chars()
    try:
        limit_bytes = _max_bytes() if max_extract_bytes is None else max(1, min(MAX_EXTRACT_BYTES, int(max_extract_bytes)))
    except Exception:
        limit_bytes = _max_bytes()
    try:
        root = _canonical_root(vault_root)
    except Exception:
        root = Path.home() / ".open-agent-os" / "wiki-vault"

    enriched: list[dict] = []
    for ref in refs:
        try:
            if not isinstance(ref, dict):
                continue
            # Image refs: preserve for the ACP image gate (no extraction).
            # Owner-unverified image refs lose their bytes (metadata-only).
            if _is_image_ref(ref):
                enriched.append(_sanitize_image_ref(ref, tenant_id, agent_principal))
                continue
            # Sensitive refs: metadata-only, never extracted; names redacted.
            if _is_sensitive_ref(ref):
                enriched.append(_sanitize_meta_ref(ref, None, tenant_id, agent_principal))
                continue
            # Durable-store gate: extraction (and prefilled text) requires
            # stored=True. Fallback/over-limit refs carry a logical
            # vault_path with no bytes — never turn it into file access.
            if ref.get("stored") is not True:
                enriched.append(_sanitize_meta_ref(ref, None, tenant_id, agent_principal))
                continue
            # Refs advertising prefilled text (e.g. retries): accept only for
            # verified owner-scoped refs — otherwise an unverified ref could
            # inject arbitrary text as a trusted citation. Bound + mask only.
            pre = ref.get("extracted_text")
            if isinstance(pre, str) and pre.strip():
                if _looks_owner_scoped(ref.get("vault_path"), tenant_id, agent_principal):
                    enriched.append(_sanitize_meta_ref(ref, mask_secrets(pre.strip())[:limit_chars], tenant_id, agent_principal))
                else:
                    enriched.append(_sanitize_meta_ref(ref, None, tenant_id, agent_principal))
                continue
            # Only extractable formats proceed; the rest stay metadata-only.
            ext = Path(str(ref.get("filename") or "")).suffix.lower()
            if ext not in _EXTRACTABLE_EXTS:
                enriched.append(_sanitize_meta_ref(ref, None, tenant_id, agent_principal))
                continue
            resolved = _resolve_owner_file(
                str(ref.get("vault_path") or ""), tenant_id, agent_principal, root
            )
            if resolved is None:
                enriched.append(_sanitize_meta_ref(ref, None, tenant_id, agent_principal))
                continue
            try:
                if not resolved.is_file():
                    enriched.append(_sanitize_meta_ref(ref, None, tenant_id, agent_principal))
                    continue
                if resolved.stat().st_size > limit_bytes:
                    enriched.append(_sanitize_meta_ref(ref, None, tenant_id, agent_principal))
                    continue
            except Exception:
                enriched.append(_sanitize_meta_ref(ref, None, tenant_id, agent_principal))
                continue
            try:
                text = await asyncio.to_thread(_extract_sync, resolved, limit_chars)
            except Exception:
                text = None
            enriched.append(_sanitize_meta_ref(ref, text, tenant_id, agent_principal))
        except Exception:
            try:
                enriched.append(_sanitize_meta_ref(ref if isinstance(ref, dict) else {}, None, tenant_id, agent_principal))
            except Exception:
                continue
    return enriched
