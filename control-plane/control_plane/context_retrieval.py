"""Owner-scoped context retrieval for Mattermost Personal Agent prompts."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class EnterpriseRetrievalError(RuntimeError):
    """Infrastructure failure during enterprise retrieval (DB/retriever unavailable).

    Distinct from an empty result: callers must not treat this as
    "no matching documents" (false fallback). Catch and mark
    retrieval as unavailable instead of grounding on parametric knowledge.
    """


_COMPANY_HINTS = ("회사", "전사", "오픈잇", "정책", "규정", "프로젝트", "사업", "조직", "매뉴얼", "문서", "현황", "업무")
_PERSONAL_HINTS = ("내 일정", "내 메일", "내 업무", "내 기록", "내 위키", "내 성향", "내가", "나의", "개인")


def classify_context_route(text: str) -> str | None:
    value = (text or "").strip().lower()
    if not value:
        return None
    if any(token in value for token in _PERSONAL_HINTS):
        return "personal"
    if any(token in value for token in _COMPANY_HINTS):
        return "enterprise"
    return None


def _wiki_root() -> Path:
    for key in ("OAOS_WIKI_VAULT", "PERSONAL_WIKI_VAULT", "VAULT_ROOT", "PERSONAL_WIKI_ROOT"):
        value = os.getenv(key, "").strip()
        if value:
            return Path(value).expanduser().resolve()
    return (Path.home() / ".open-agent-os" / "wiki-vault").resolve()


def _personal_candidates(user_id: str) -> list[Path]:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in user_id)[:96]
    root = _wiki_root()
    return [root / "journal", root / "notes", root / "projects", root / "files", root / "attachments", root / safe]


def _read_personal_sync(user_id: str, query: str, max_files: int = 5) -> list[dict[str, Any]]:
    query_terms = [x for x in query.lower().split() if len(x) > 1]
    found: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for directory in _personal_candidates(user_id):
        if not directory.exists() or not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True):
            if path in seen or len(found) >= max_files:
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            low = text.lower()
            if not query_terms or any(term in low for term in query_terms):
                found.append({"title": path.name, "path": str(path.relative_to(_wiki_root())), "text": text[-3500:]})
    return found


async def retrieve_personal_context(user_id: str, query: str) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_read_personal_sync, user_id, query)


def _ensure_repo_root_on_path() -> None:
    """Make root-level knowledge_index/security importable in-process (no new service)."""
    try:
        repo_root = Path(__file__).resolve().parents[3]
    except Exception:
        return
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


async def retrieve_enterprise_context(
    tenant_id: str,
    agent_id: str,
    query: str,
    limit: int = 5,
    *,
    allowed_group_ids: list[str] | None = None,
    user_id: str | None = None,
    repository: Any | None = None,
    session_maker: Any | None = None,
) -> list[dict[str, Any]]:
    """Owner/tenant/group-aware enterprise retrieval over the persistent index.

    Uses the existing in-process KnowledgeIndexRepository +
    KnowledgeIndexRetriever (lexical, read-only SELECT) — no direct DB
    session handling here, no cross-tenant fallback, no mock/hash fallback.

    ACL (enforced in SQL before retrieval):
      - tenant_id mandatory (ValueError if missing) — never falls back
        to another tenant's rows.
      - agent allow-list is exactly [agent_id]: public rows
        (group_id AND agent_id IS NULL) plus rows scoped to this owner.
        Other owners' agent-scoped rows are never returned.
      - group allow-list is caller-supplied (verified membership only).
        Default/empty is fail-closed: group-scoped rows are hidden unless
        the caller proves membership via allowed_group_ids.

    Provenance is always preserved in each hit dict.

    Failure behavior: empty query returns []. Infrastructure failures
    (DB/retriever unavailable) raise EnterpriseRetrievalError — never a
    silent [] — so callers can mark retrieval unavailable instead of
    treating an outage as "no matching documents".
    """
    if not tenant_id or not str(tenant_id).strip():
        raise ValueError("tenant_id is required — cross-tenant isolation violation")
    if not agent_id or not str(agent_id).strip():
        raise ValueError("agent_id is required — owner scope violation")
    if not query or not query.strip():
        return []
    tenant_id = str(tenant_id).strip()
    agent_id = str(agent_id).strip()
    groups = [str(g).strip() for g in (allowed_group_ids or []) if str(g).strip()]
    bounded = max(1, min(int(limit or 5), 10))

    _ensure_repo_root_on_path()
    try:
        if repository is None:
            from knowledge_index.repository import KnowledgeIndexRepository
            from security.models.db import get_sessionmaker

            maker = session_maker or get_sessionmaker()
            repository = KnowledgeIndexRepository(maker)
        from knowledge_index.retrieval import KnowledgeIndexRetriever

        retriever = KnowledgeIndexRetriever(repository)
        hits = await retriever.retrieve(
            query=query.strip(),
            tenant_id=tenant_id,
            allowed_group_ids=groups,
            allowed_agent_ids=[agent_id],
            limit=bounded,
            mode="lexical",
        )
    except ValueError:
        raise
    except Exception as exc:
        log.warning(
            "enterprise retrieval unavailable tenant=%s agent=%s: %s",
            tenant_id, agent_id, type(exc).__name__,
        )
        raise EnterpriseRetrievalError(
            f"enterprise retrieval unavailable: {type(exc).__name__}"
        ) from exc
    _ = user_id  # accepted for audit parity; ACL owner is agent_id allow-list
    return [
        {
            "title": hit.source_resource_id,
            "source_uri": hit.source_uri,
            "text": (hit.chunk_text or "")[:3500],
            "score": hit.score,
            "provenance": hit.provenance,
            "tenant_id": hit.tenant_id,
            "group_id": hit.group_id,
            "agent_id": hit.agent_id,
            "source_resource_id": hit.source_resource_id,
            "index_id": hit.index_id,
        }
        for hit in hits
    ]


def format_context(route: str, hits: list[dict[str, Any]]) -> str:
    if not hits:
        return ""
    label = "개인 위키 검색 결과" if route == "personal" else "회사 Knowledge Index 검색 결과"
    lines = [f"[OAOS {label} — 아래 내용을 근거로 사용하고, 근거가 부족하면 모른다고 답변] "]
    for index, hit in enumerate(hits, 1):
        source = hit.get("source_uri") or hit.get("path") or hit.get("title") or "unknown"
        lines.append(f"[{index}] source={source}\n{hit.get('text', '')}")
    return "\n\n".join(lines)
