"""Focused tests for Outline collection ACL resolution (read-only, fake transport).

Covers knowledge_index/outline_acl.py + its sync_outline_to_index wiring:
- read/read_write collection permission -> tenant-public (acl {})
- admin/null permission -> members-only agent allow-list from active users
- email local-part -> deterministic agent principal (valid emails only)
- suspended/deleted/invalid-email users skipped, IDs/emails kept in
  provenance without secrets
- fail-closed on resolution failure (strict/production): sentinel restricted
  ACL, never public
- sync persistence: private docs land as agent-restricted entries (no public
  null/null row); member finds, stranger misses

No live network, no production DB mutation (sqlite memory only).
"""
from __future__ import annotations

import pytest

from knowledge_index.models import SourceDocument


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class RoutingFakeTransport:
    """Fake Outline transport routing queued responses by API path suffix."""

    def __init__(self, by_path: dict | None = None, fail_paths: set | None = None):
        self.by_path: dict[str, list] = {k: list(v) for k, v in (by_path or {}).items()}
        self.fail_paths: set[str] = set(fail_paths or ())
        self.calls: list[dict] = []

    def _path(self, url: str) -> str:
        for suffix in (
            "/api/documents.list",
            "/api/collections.info",
            "/api/collections.list",
            "/api/collections.memberships",
            "/api/users.list",
        ):
            if url.endswith(suffix):
                return suffix
        return url

    def post(self, url, headers=None, json=None, timeout=None):
        path = self._path(url)
        self.calls.append({"url": url, "path": path, "headers": dict(headers or {}), "json": json})
        if path in self.fail_paths:
            raise RuntimeError(f"injected transport failure for {path}")
        queue = self.by_path.get(path)
        if not queue:
            raise RuntimeError(f"no queued response for {path}")
        nxt = queue.pop(0)
        if isinstance(nxt, FakeResp):
            return nxt
        return FakeResp(200, nxt)


def _adapter(tr):
    from knowledge_index.connectors.http_outline import HttpOutlineSourceAdapter

    return HttpOutlineSourceAdapter(
        api_url="https://o.example.com",
        api_token="tok-secret",
        http_client=tr,
        retry_backoff_s=0.001,
    )


def _doc(collection="col_private", doc_id="doc_001", acl=None):
    return SourceDocument(
        resource_id=f"outline/{collection}/{doc_id}",
        source_system="outline",
        title="T",
        content="hello world acl test",
        source_updated_at="2026-01-01T00:00:00+00:00",
        acl_version="v1",
        acl=dict(acl or {}),
        source_uri=f"https://o.example.com/doc/{doc_id}",
    )


def _private_transport(permission=None):
    return RoutingFakeTransport(
        by_path={
            "/api/collections.info": [{"data": {"id": "col_private", "name": "Private", "permission": permission}}],
            "/api/collections.memberships": [
                {
                    "data": [
                        {"userId": "u-alice", "user": {"id": "u-alice", "email": "Alice@Example.com"}},
                        {"userId": "u-bob"},
                        {"userId": "u-suspended"},
                        {"userId": "u-deleted"},
                        {"userId": "u-noemail"},
                        {"userId": "u-bademail"},
                    ],
                    "pagination": {"offset": 0, "total": 6},
                }
            ],
            "/api/users.list": [
                {
                    "data": [
                        {"id": "u-alice", "email": "Alice@Example.com"},
                        {"id": "u-bob", "email": "bob@example.com"},
                        {"id": "u-suspended", "email": "suspended@example.com", "isSuspended": True},
                        {"id": "u-deleted", "email": "gone@example.com", "deletedAt": "2026-01-02T00:00:00Z"},
                        {"id": "u-noemail", "email": ""},
                        {"id": "u-bademail", "email": "not-an-email"},
                    ],
                    "pagination": {"offset": 0, "total": 6},
                }
            ],
        }
    )


# ---------------------------------------------------------------------------
# mapping unit tests
# ---------------------------------------------------------------------------
class TestEmailMapping:
    def test_valid_email_maps_to_deterministic_principal(self):
        from knowledge_index.outline_acl import agent_principal_for_email

        assert agent_principal_for_email("Alice@Example.com") == "agent:assistant:alice"
        assert agent_principal_for_email("  BOB@Example.com  ") == "agent:assistant:bob"
        assert agent_principal_for_email("first.last+tag@example.co.kr") == "agent:assistant:first.last+tag"

    def test_invalid_email_returns_none(self):
        from knowledge_index.outline_acl import agent_principal_for_email

        for bad in (None, "", "   ", "no-at-sign", "@nodomain", "nolocal@", "a@b", "a b@c.com", 123):
            assert agent_principal_for_email(bad) is None


# ---------------------------------------------------------------------------
# collection permission semantics
# ---------------------------------------------------------------------------
class TestCollectionPermissionSemantics:
    @pytest.mark.parametrize("permission", ["read", "read_write", "READ", "Read_Write"])
    async def test_public_permissions_are_tenant_public(self, permission):
        from knowledge_index.outline_acl import OutlineACLResolver

        tr = RoutingFakeTransport(
            by_path={"/api/collections.info": [{"data": {"id": "col_open", "permission": permission}}]}
        )
        resolver = OutlineACLResolver(_adapter(tr))
        enriched, prov = resolver.enrich_documents([_doc(collection="col_open")], on_error="strict")
        assert enriched[0].acl == {}
        assert prov[enriched[0].resource_id]["outline_acl_mode"] == "tenant-public"
        assert prov[enriched[0].resource_id]["outline_collection_permission"] == permission.strip().lower()
        # membership/user endpoints never needed for public collections
        assert not [c for c in tr.calls if c["path"] in ("/api/collections.memberships", "/api/users.list")]

    @pytest.mark.parametrize("permission", [None, "admin", "ADMIN"])
    async def test_restricted_permissions_are_members_only(self, permission):
        from knowledge_index.outline_acl import OutlineACLResolver

        resolver = OutlineACLResolver(_adapter(_private_transport(permission)))
        enriched, prov = resolver.enrich_documents([_doc()], on_error="strict")
        assert enriched[0].acl == {"users": ["agent:assistant:alice", "agent:assistant:bob"]}
        p = prov[enriched[0].resource_id]
        assert p["outline_acl_mode"] == "members-only"
        assert p["outline_acl_unresolved"] is False
        assert p["outline_member_count"] == 2
        # suspended/deleted/invalid-email users never in ACL...
        assert "agent:suspended" not in enriched[0].acl["users"]
        assert "agent:gone" not in enriched[0].acl["users"]
        # ...but outline IDs/emails of mapped members preserved without secrets
        assert "u-alice" in p["outline_member_user_ids"]
        assert "Alice@Example.com" in p["outline_member_emails"]
        blob = str(p)
        assert "tok-secret" not in blob
        assert "Bearer" not in blob
        # acl_version reflects resolved membership (reindex on permission/member change)
        assert "~cac:" in enriched[0].acl_version

    async def test_doc_level_groups_preserved_for_private(self):
        from knowledge_index.outline_acl import OutlineACLResolver

        resolver = OutlineACLResolver(_adapter(_private_transport(None)))
        enriched, _ = resolver.enrich_documents(
            [_doc(acl={"groups": ["eng"]})], on_error="strict"
        )
        assert enriched[0].acl["groups"] == ["eng"]
        assert enriched[0].acl["users"] == ["agent:assistant:alice", "agent:assistant:bob"]

    async def test_collection_metadata_cached_across_docs(self):
        from knowledge_index.outline_acl import OutlineACLResolver

        resolver = OutlineACLResolver(_adapter(_private_transport(None)))
        enriched, _ = resolver.enrich_documents([_doc(doc_id="d1"), _doc(doc_id="d2")], on_error="strict")
        assert len(enriched) == 2
        info_calls = [c for c in resolver.calls if c["path"] == "/api/collections.info"]
        assert len(info_calls) == 1


# ---------------------------------------------------------------------------
# fail-closed
# ---------------------------------------------------------------------------
class TestFailClosed:
    async def test_strict_failure_is_sentinel_restricted_never_public(self):
        from knowledge_index.outline_acl import UNRESOLVED_AGENT_SENTINEL, OutlineACLResolver

        tr = RoutingFakeTransport(fail_paths={"/api/collections.info", "/api/collections.list"})
        resolver = OutlineACLResolver(_adapter(tr))
        enriched, prov = resolver.enrich_documents([_doc()], on_error="strict")
        assert enriched[0].acl["users"] == [UNRESOLVED_AGENT_SENTINEL]
        p = prov[enriched[0].resource_id]
        assert p["outline_acl_mode"] == "unresolved-restricted"
        assert p["outline_acl_unresolved"] is True
        assert "tok-secret" not in str(p)

    async def test_auto_is_strict_in_production(self, monkeypatch):
        from knowledge_index.outline_acl import UNRESOLVED_AGENT_SENTINEL, OutlineACLResolver

        monkeypatch.setenv("OAOS_ENV", "production")
        tr = RoutingFakeTransport(fail_paths={"/api/collections.info", "/api/collections.list"})
        resolver = OutlineACLResolver(_adapter(tr))
        enriched, prov = resolver.enrich_documents([_doc()])
        assert enriched[0].acl["users"] == [UNRESOLVED_AGENT_SENTINEL]
        assert prov[enriched[0].resource_id]["outline_acl_unresolved"] is True

    async def test_passthrough_keeps_source_acl_only_outside_production(self, monkeypatch):
        from knowledge_index.outline_acl import OutlineACLResolver

        for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
            monkeypatch.delenv(k, raising=False)
        tr = RoutingFakeTransport(fail_paths={"/api/collections.info", "/api/collections.list"})
        resolver = OutlineACLResolver(_adapter(tr))
        enriched, prov = resolver.enrich_documents([_doc(acl={"groups": ["eng"]})], on_error="passthrough")
        assert enriched[0].acl == {"groups": ["eng"]}
        assert prov[enriched[0].resource_id]["outline_acl_mode"] == "passthrough-unresolved"

    async def test_empty_member_set_is_restricted_not_public(self):
        from knowledge_index.outline_acl import UNRESOLVED_AGENT_SENTINEL, OutlineACLResolver

        tr = RoutingFakeTransport(
            by_path={
                "/api/collections.info": [{"data": {"id": "col_private", "permission": None}}],
                "/api/collections.memberships": [{"data": [], "pagination": {"offset": 0, "total": 0}}],
                "/api/users.list": [{"data": [], "pagination": {"offset": 0, "total": 0}}],
            }
        )
        resolver = OutlineACLResolver(_adapter(tr))
        enriched, _ = resolver.enrich_documents([_doc()], on_error="strict")
        # empty member set must NOT collapse to tenant-public {}
        assert enriched[0].acl["users"] == [UNRESOLVED_AGENT_SENTINEL]


# ---------------------------------------------------------------------------
# sync persistence wiring
# ---------------------------------------------------------------------------
async def _sqlite_repo():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from knowledge_index.orm import KnowledgeIndexORM

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: KnowledgeIndexORM.__table__.create(sc, checkfirst=True))
    return async_sessionmaker(engine, expire_on_commit=False), engine


class TestSyncPersistenceWiring:
    async def test_private_doc_persists_as_agent_restricted_only(self):
        from knowledge_index.embedding import FakeEmbeddingProvider
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.retrieval import KnowledgeIndexRetriever
        from knowledge_index.service import KnowledgeSearchService, sync_outline_to_index

        raw = {
            "id": "doc_001",
            "collectionId": "col_private",
            "title": "Private plan",
            "text": "classified syllabus content",
            "updatedAt": "2026-01-01T00:00:00Z",
        }
        tr = RoutingFakeTransport(
            by_path={
                "/api/documents.list": [{"data": [raw], "pagination": {"offset": 0, "total": 1}}],
                "/api/collections.info": [{"data": {"id": "col_private", "permission": None}}],
                "/api/collections.memberships": [
                    {"data": [{"userId": "u-alice"}], "pagination": {"offset": 0, "total": 1}}
                ],
                "/api/users.list": [
                    {"data": [{"id": "u-alice", "email": "alice@example.com"}],
                     "pagination": {"offset": 0, "total": 1}}
                ],
            }
        )
        maker, engine = await _sqlite_repo()
        try:
            repo = KnowledgeIndexRepository(maker)
            result = await sync_outline_to_index(
                tenant_id="tenant-a",
                repository=repo,
                embedding_provider=FakeEmbeddingProvider(dim=8),
                outline_adapter=_adapter(tr),
                acl_on_error="strict",
            )
            assert result.fetched == 1
            assert result.persisted >= 1
            rows = await repo.list_by_tenant("tenant-a", limit=50)
            rids = [r.source_resource_id for r in rows]
            assert rids and all(r == "outline/col_private/doc_001" for r in rids)
            # no tenant-public (null/null) row for the private doc
            assert not [r for r in rows if r.group_id is None and r.agent_id is None]
            assert {r.agent_id for r in rows} == {"agent:assistant:alice"}
            # provenance carries outline membership without secrets
            prov = rows[0].provenance or {}
            assert prov.get("outline_acl_mode") == "members-only"
            assert "u-alice" in (prov.get("outline_member_user_ids") or [])
            assert "tok-secret" not in str(prov)

            retr = KnowledgeIndexRetriever(repo)
            svc = KnowledgeSearchService(retr)
            hits_member = await svc.search(
                query="syllabus", tenant_id="tenant-a", user_id="agent:assistant:alice", limit=10
            )
            assert any("syllabus" in h.chunk_text for h in hits_member)
            hits_stranger = await svc.search(
                query="syllabus", tenant_id="tenant-a", user_id="agent:stranger", limit=10
            )
            assert all("syllabus" not in h.chunk_text for h in hits_stranger)
        finally:
            await engine.dispose()

    async def test_public_doc_persists_as_tenant_public(self):
        from knowledge_index.embedding import FakeEmbeddingProvider
        from knowledge_index.repository import KnowledgeIndexRepository
        from knowledge_index.retrieval import KnowledgeIndexRetriever
        from knowledge_index.service import KnowledgeSearchService, sync_outline_to_index

        raw = {
            "id": "doc_pub",
            "collectionId": "col_open",
            "title": "Handbook",
            "text": "public handbook content",
            "updatedAt": "2026-01-01T00:00:00Z",
        }
        tr = RoutingFakeTransport(
            by_path={
                "/api/documents.list": [{"data": [raw], "pagination": {"offset": 0, "total": 1}}],
                "/api/collections.info": [{"data": {"id": "col_open", "permission": "read"}}],
            }
        )
        maker, engine = await _sqlite_repo()
        try:
            repo = KnowledgeIndexRepository(maker)
            result = await sync_outline_to_index(
                tenant_id="tenant-a",
                repository=repo,
                embedding_provider=FakeEmbeddingProvider(dim=8),
                outline_adapter=_adapter(tr),
                acl_on_error="strict",
            )
            assert result.persisted >= 1
            rows = await repo.list_by_tenant("tenant-a", limit=50)
            assert [r for r in rows if r.group_id is None and r.agent_id is None]
            retr = KnowledgeIndexRetriever(repo)
            svc = KnowledgeSearchService(retr)
            hits = await svc.search(
                query="handbook", tenant_id="tenant-a", user_id="agent:anyone", limit=10
            )
            assert any("handbook" in h.chunk_text for h in hits)
        finally:
            await engine.dispose()
