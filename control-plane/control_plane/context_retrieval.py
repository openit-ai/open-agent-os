"""Owner-scoped context retrieval for Mattermost Personal Agent prompts."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select


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


async def retrieve_enterprise_context(tenant_id: str, agent_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Lexical ACL-prefiltered retrieval; no LLM and no cross-tenant fallback."""
    if not tenant_id or not agent_id or not query.strip():
        return []
    try:
        from security.models.db import get_sessionmaker
        from knowledge_index.orm import KnowledgeIndexORM
        maker = get_sessionmaker()
        terms = [term for term in query.strip().split() if len(term) > 1][:8]
        clauses = [KnowledgeIndexORM.tenant_id == tenant_id]
        acl = or_(
            (KnowledgeIndexORM.group_id.is_(None) & KnowledgeIndexORM.agent_id.is_(None)),
            KnowledgeIndexORM.agent_id == agent_id,
        )
        text_clauses = [KnowledgeIndexORM.chunk_text.ilike(f"%{term}%") for term in terms]
        async with maker() as session:
            stmt = select(KnowledgeIndexORM).where(*clauses).where(acl)
            if text_clauses:
                stmt = stmt.where(or_(*text_clauses))
            result = await session.execute(stmt.limit(max(1, min(limit, 10))))
            return [
                {"title": row.source_resource_id, "source_uri": row.source_uri, "text": row.chunk_text[:3500], "score": None, "provenance": row.provenance}
                for row in result.scalars().all()
            ]
    except Exception:
        return []


def format_context(route: str, hits: list[dict[str, Any]]) -> str:
    if not hits:
        return ""
    label = "개인 위키 검색 결과" if route == "personal" else "회사 Knowledge Index 검색 결과"
    lines = [f"[OAOS {label} — 아래 내용을 근거로 사용하고, 근거가 부족하면 모른다고 답변] "]
    for index, hit in enumerate(hits, 1):
        source = hit.get("source_uri") or hit.get("path") or hit.get("title") or "unknown"
        lines.append(f"[{index}] source={source}\n{hit.get('text', '')}")
    return "\n\n".join(lines)
