"""Personal Wiki auto-archive helper — best-effort, non-blocking, no hard deps.

Used by execution-gateway proxy and app to write journal md with trace_id + tool_name + truncated result.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _try_import_append_journal():
    """Lazy import append_journal with sys.path fallback for packages/personal-wiki."""
    try:
        from personal_wiki.vault import append_journal  # type: ignore
        return append_journal
    except Exception:
        pass
    # Fallback: add packages/personal-wiki to sys.path
    try:
        # locate repo root by walking up from this file
        here = Path(__file__).resolve()
        for parent in here.parents:
            candidate = parent / "packages" / "personal-wiki"
            if (candidate / "personal_wiki" / "vault.py").exists():
                if str(candidate) not in sys.path:
                    sys.path.insert(0, str(candidate))
                try:
                    from personal_wiki.vault import append_journal  # type: ignore
                    return append_journal
                except Exception:
                    return None
            # also try open-agent-os root
            if (parent / "packages" / "personal-wiki" / "personal_wiki" / "vault.py").exists():
                cand = parent / "packages" / "personal-wiki"
                if str(cand) not in sys.path:
                    sys.path.insert(0, str(cand))
                try:
                    from personal_wiki.vault import append_journal  # type: ignore
                    return append_journal
                except Exception:
                    return None
    except Exception:
        pass
    return None


def auto_archive(trace_id: str, tool_name: str, result: Any, max_chars: int = 4000) -> None:
    """Best-effort write journal md. Never raises."""
    try:
        # disable switch
        if os.getenv("OAOS_WIKI_AUTO_ARCHIVE", "1") in ("0", "false", "False", "off"):
            return
        # skip empty
        if not trace_id and not tool_name:
            return
        append_journal = _try_import_append_journal()
        if append_journal is None:
            return
        # truncate via vault's own max_chars; pass truncated here too for safety
        append_journal(
            trace_id=trace_id or "unknown",
            tool_name=tool_name or "unknown",
            result=result,
            max_chars=max_chars,
        )
    except Exception:
        pass
