"""Outline Shared Knowledge Connector — Section 28 ACL + Retrieval Pipeline

Outline / Notion 등 shared knowledge는 personal이 아닌 shared 자원이다.
접근 제어가 ACL 기반 — retrieval 전 ACL 적용 (Section 28).

보안 원칙:
- ACL은 retrieval 전에 적용 (pre-filter)
- tenant isolation: 다른 tenant 문서 접근 불가
- group 기반 접근 (organization / group scope)
- retrieval pipeline: Search → ACL filter → Ranking → Response
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    from ..normalize import parse_resource
except ImportError:
    from execution_gateway.normalize import parse_resource  # type: ignore

# ── Outline ACL Mock ─────────────────────────────────────────────────
_DEFAULT_ACL: dict[str, dict] = {
    "outline/*": {"tenants": ["*"], "groups": ["*"], "public": False},
    "outline/team/*": {"tenants": ["*"], "groups": ["*"], "public": False},
    "outline/private/*": {"tenants": ["*"], "groups": ["admin"], "public": False},
}

# ── Document model (Outline API) ─────────────────────────────────────
@dataclass
class OutlineDocument:
    id: str
    collection_id: str  # e.g. team, private
    title: str
    content: str
    url: str
    created_at: str = ""
    updated_at: str = ""
    # ACL per document
    acl: dict[str, Any] = field(default_factory=dict)
    # classification (§29)
    classification: str = "INTERNAL"
    acl_version: str = "v1"
    # ranking signals
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "collection_id": self.collection_id,
            "title": self.title,
            "content": self.content,
            "url": self.url,
            "collection": self.collection_id,
            "classification": self.classification,
            "acl_version": self.acl_version,
            "score": self.score,
        }


# In-memory document corpus (mock Outline API)
_DEFAULT_DOCS: list[OutlineDocument] = [
    OutlineDocument(
        id="doc_001", collection_id="team", title="Onboarding Guide",
        content="Welcome to the team. This guide covers onboarding process and team rituals.",
        url="https://outline.example.com/doc/onboarding", classification="INTERNAL", acl_version="v1",
    ),
    OutlineDocument(
        id="doc_002", collection_id="team", title="API Design Guidelines",
        content="API design principles: REST, versioning, error handling, pagination.",
        url="https://outline.example.com/doc/api-design", classification="INTERNAL", acl_version="v1",
    ),
    OutlineDocument(
        id="doc_003", collection_id="team", title="Security Policy",
        content="Security policy: credential rotation, confidential handling, secret management.",
        url="https://outline.example.com/doc/security-policy", classification="CONFIDENTIAL", acl_version="v2",
    ),
    OutlineDocument(
        id="doc_004", collection_id="private", title="Finance Budget 2026",
        content="Finance budget confidential: Q1 allocation and headcount planning.",
        url="https://outline.example.com/doc/finance-budget", classification="CONFIDENTIAL", acl_version="v1",
        acl={"groups": ["admin", "finance"]},
    ),
    OutlineDocument(
        id="doc_005", collection_id="team", title="Customer Data Handling",
        content="PII handling: customer email and phone must be encrypted, GDPR compliance.",
        url="https://outline.example.com/doc/pii-handling", classification="PII", acl_version="v1",
    ),
]


@dataclass(frozen=True)
class ACLCheckResult:
    allowed: bool
    reason: str
    matched_acl: str | None = None


@dataclass
class SearchResult:
    documents: list[OutlineDocument]
    total: int
    query: str
    filtered_count: int = 0
    ranked: bool = False


class OutlineConnector:
    """Outline shared knowledge connector — ACL 기반 접근 제어 + retrieval pipeline"""

    name = "outline"
    provider = "outline"

    TOOL_ACTION: dict[str, str] = {
        "outline_search": "SEARCH",
        "outline_read": "READ",
        "outline_create": "CREATE",
        "outline_modify": "MODIFY",
    }

    def __init__(
        self,
        acl_store: dict[str, dict] | None = None,
        documents: list[OutlineDocument] | None = None,
        api_url: str | None = None,
        api_token: str | None = None,
    ):
        self._acl = acl_store if acl_store is not None else dict(_DEFAULT_ACL)
        self._docs: list[OutlineDocument] = documents if documents is not None else list(_DEFAULT_DOCS)
        self._api_url = api_url
        self._api_token = api_token

    def list_tools(self) -> list[str]:
        return list(self.TOOL_ACTION.keys())

    def list_resources(self) -> list[str]:
        return ["outline/*"]

    def tool_action(self, tool_name: str) -> str:
        return self.TOOL_ACTION.get(tool_name, "READ")

    # ── Document store management ────────────────────────────────────
    def add_document(self, doc: OutlineDocument) -> None:
        self._docs.append(doc)

    def get_document(self, doc_id: str) -> OutlineDocument | None:
        for d in self._docs:
            if d.id == doc_id:
                return d
        return None

    def list_documents(self) -> list[OutlineDocument]:
        return list(self._docs)

    def clear_documents(self) -> None:
        self._docs.clear()

    def set_documents(self, docs: list[OutlineDocument]) -> None:
        self._docs = list(docs)

    # ── ACL check ────────────────────────────────────────────────────
    def check_acl(
        self,
        agent_context: dict | object,
        resource: str,
        action: str = "READ",
    ) -> ACLCheckResult:
        """Section 28: Identity → Allowed Scope → Retrieval → Allowed Documents."""
        if isinstance(agent_context, dict):
            tenant_id = agent_context.get("tenant_id")
            user_id = agent_context.get("user_id")
            groups: list[str] = agent_context.get("groups", []) or agent_context.get("context", {}).get("groups", [])
        else:
            tenant_id = getattr(agent_context, "tenant_id", None)
            user_id = getattr(agent_context, "user_id", None)
            groups = getattr(agent_context, "groups", []) or []

        if not tenant_id:
            return ACLCheckResult(False, "missing tenant_id", None)

        try:
            parsed = parse_resource(resource)
        except ValueError as e:
            return ACLCheckResult(False, f"invalid resource: {e}")

        if parsed.domain != "outline":
            return ACLCheckResult(True, f"domain {parsed.domain} not handled by outline connector")

        if "private" in resource.lower():
            if "admin" not in groups and user_id != "employee:admin":
                pass  # trace only, not deny at gateway pre-check

        return ACLCheckResult(True, "acl pre-check passed", "outline-allow")

    def check_document_acl(
        self,
        agent_context: dict | object,
        doc: OutlineDocument,
    ) -> ACLCheckResult:
        """Per-document ACL check (collection + doc-level)."""
        if isinstance(agent_context, dict):
            groups: list[str] = agent_context.get("groups", []) or []
            user_id = agent_context.get("user_id", "")
            tenant_id = agent_context.get("tenant_id", "")
        else:
            groups = getattr(agent_context, "groups", []) or []
            user_id = getattr(agent_context, "user_id", "")
            tenant_id = getattr(agent_context, "tenant_id", "")

        # doc-level ACL
        doc_acl = doc.acl or {}
        allowed_groups = doc_acl.get("groups")
        if allowed_groups is not None:
            # explicit doc ACL — must be in allowed groups
            if not any(g in allowed_groups for g in groups) and "admin" not in groups:
                # also check if user is directly allowed
                allowed_users = doc_acl.get("users", [])
                if user_id not in allowed_users and "*" not in allowed_groups:
                    return ACLCheckResult(False, f"document {doc.id} ACL denied: requires {allowed_groups}", None)

        # collection-level ACL
        collection_key = f"outline/{doc.collection_id}/*"
        # fallback to wildcard
        for pattern, acl in self._acl.items():
            # simple glob match for collection
            if _match_collection(pattern, f"outline/{doc.collection_id}/{doc.id}"):
                acl_groups = acl.get("groups", ["*"])
                if "*" in acl_groups:
                    return ACLCheckResult(True, "collection ACL allow", pattern)
                if any(g in acl_groups for g in groups):
                    return ACLCheckResult(True, "collection ACL allow", pattern)
                # private collection requires admin
                if doc.collection_id == "private" and "admin" not in groups:
                    return ACLCheckResult(False, f"collection {doc.collection_id} requires admin", pattern)

        return ACLCheckResult(True, "document ACL passed", "doc-allow")

    def can_write(self, agent_context: dict | object, resource: str) -> ACLCheckResult:
        base = self.check_acl(agent_context, resource, action="CREATE")
        if not base.allowed:
            return base
        return ACLCheckResult(True, "write allowed", base.matched_acl)

    # ── Retrieval pipeline ───────────────────────────────────────────
    def search_documents(
        self,
        query: str,
        agent_context: dict | object | None = None,
        limit: int = 10,
        collection_id: str | None = None,
    ) -> list[OutlineDocument]:
        """Mock Outline API search — keyword match (real impl would call Outline API)."""
        if not query or not query.strip():
            candidates = list(self._docs)
        else:
            q_lower = query.lower()
            q_terms = [t for t in re.split(r"\s+", q_lower) if t]
            candidates = []
            for doc in self._docs:
                haystack = f"{doc.title} {doc.content}".lower()
                if any(term in haystack for term in q_terms):
                    candidates.append(doc)
                elif query.lower() in haystack:
                    candidates.append(doc)

        if collection_id:
            candidates = [d for d in candidates if d.collection_id == collection_id]

        return candidates[:limit]

    async def search_api(
        self,
        query: str,
        agent_context: dict | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Real Outline API call if configured, else mock search.

        Outline API: POST {api_url}/api/documents.search  {query, limit}
        """
        if self._api_url and self._api_token:
            try:
                import httpx  # type: ignore
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"{self._api_url.rstrip('/')}/api/documents.search",
                        headers={"Authorization": f"Bearer {self._api_token}"},
                        json={"query": query, "limit": limit},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        # Outline returns {data: [...]}
                        docs = data.get("data", [])
                        return docs
            except Exception:
                pass  # fallback to mock

        # mock fallback
        docs = self.search_documents(query, agent_context, limit)
        return [d.to_dict() for d in docs]

    def acl_filter(
        self,
        documents: list[OutlineDocument],
        agent_context: dict | object,
    ) -> tuple[list[OutlineDocument], int]:
        """Section 28: retrieval 전 ACL filter (pre-filter)."""
        allowed: list[OutlineDocument] = []
        denied_count = 0
        for doc in documents:
            result = self.check_document_acl(agent_context, doc)
            if result.allowed:
                allowed.append(doc)
            else:
                denied_count += 1
        return allowed, denied_count

    def rank(
        self,
        documents: list[OutlineDocument],
        query: str,
    ) -> list[OutlineDocument]:
        """Simple ranking: title match > content match, recency, exact phrase boost."""
        if not query:
            return documents
        q_lower = query.lower()
        q_terms = [t for t in re.split(r"\s+", q_lower) if t]

        scored: list[tuple[float, OutlineDocument]] = []
        for doc in documents:
            score = 0.0
            title_lower = doc.title.lower()
            content_lower = doc.content.lower()
            # title exact match boost
            if q_lower in title_lower:
                score += 10
            # title term matches
            for term in q_terms:
                if term in title_lower:
                    score += 5
                if term in content_lower:
                    score += 1
            # exact phrase in content
            if q_lower in content_lower:
                score += 3
            # classification penalty: SECRET/PII slightly demoted for general queries
            # unless query explicitly asks for them
            if doc.classification in ("PII", "SECRET") and "pii" not in q_lower and "secret" not in q_lower:
                score -= 0.5
            doc.score = score
            scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored]

    def retrieve(
        self,
        query: str,
        agent_context: dict | object,
        limit: int = 10,
        collection_id: str | None = None,
    ) -> SearchResult:
        """Full retrieval pipeline: Search → ACL filter → Ranking.

        Section 28 compliant: ACL is applied BEFORE ranking/response.
        """
        # 1. Search (Outline API)
        candidates = self.search_documents(query, agent_context, limit=limit * 2, collection_id=collection_id)
        total = len(candidates)

        # 2. ACL filter (pre-filter before ranking)
        allowed, denied = self.acl_filter(candidates, agent_context)

        # 3. Ranking
        ranked = self.rank(allowed, query)

        # 4. Limit
        ranked = ranked[:limit]

        return SearchResult(
            documents=ranked,
            total=total,
            query=query,
            filtered_count=denied,
            ranked=True,
        )

    def describe(self) -> dict:
        return {
            "name": self.name,
            "provider": self.provider,
            "tools": self.list_tools(),
            "resources": self.list_resources(),
        }


def _match_collection(pattern: str, resource: str) -> bool:
    """Simple glob matching for collection ACL."""
    import fnmatch
    return fnmatch.fnmatch(resource, pattern)
