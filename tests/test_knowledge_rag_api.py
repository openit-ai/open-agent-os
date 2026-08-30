"""HTTP integration for knowledge RAG — stepwise search + sync materialization.

Wraps memory_service /v1/knowledge/* endpoints (no live Outline creds).
Uses injected documents for sync and X-User-Id fixture auth.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PYTEST_CURRENT_TEST", "1")


def _load_app():
    # Ensure knowledge_index importable
    for _cand in (str(ROOT / "packages" / "knowledge-index"), str(ROOT)):
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
    spec = importlib.util.spec_from_file_location("memory_service.app_rag_test", str(ROOT / "memory_service" / "app.py"))
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    assert spec and spec.loader
    spec.loader.exec_module(mod)  # type: ignore
    return mod.app


@pytest.fixture(scope="module")
def client():
    app = _load_app()
    with TestClient(app) as c:
        yield c


def _hdr(user="employee:alice", tenant="tenant-a", groups=""):
    h = {"X-User-Id": user, "X-Tenant-Id": tenant}
    if groups:
        h["X-Groups"] = groups
    return h


def test_health(client):
    r = client.get("/v1/knowledge/health")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok"
    assert j["service"] == "knowledge-index"


def test_search_requires_auth(client):
    # no headers -> 401
    r = client.post("/v1/knowledge/search", json={"query": "hello"})
    assert r.status_code in (401, 422)


def test_search_tenant_isolation(client):
    # sync two tenants with distinct docs via injected documents
    r = client.post("/v1/knowledge/sync", json={"tenant_id": "tenant-iso-a", "documents": [{"resource_id": "outline/team/doc_iso_a", "title": "Iso A", "content": "unique iso alpha token", "acl": {}}]}, headers=_hdr(tenant="tenant-iso-a"))
    assert r.status_code == 200, r.text
    r = client.post("/v1/knowledge/sync", json={"tenant_id": "tenant-iso-b", "documents": [{"resource_id": "outline/team/doc_iso_b", "title": "Iso B", "content": "unique iso alpha token", "acl": {}}]}, headers=_hdr(tenant="tenant-iso-b"))
    assert r.status_code == 200, r.text
    # search tenant-a should not see tenant-b
    ra = client.post("/v1/knowledge/search", json={"query": "unique iso alpha", "limit": 10}, headers=_hdr(tenant="tenant-iso-a"))
    assert ra.status_code == 200
    ids_a = {x["source_resource_id"] for x in ra.json()["results"]}
    assert "outline/team/doc_iso_a" in ids_a
    assert "outline/team/doc_iso_b" not in ids_a
    rb = client.post("/v1/knowledge/search", json={"query": "unique iso alpha", "limit": 10}, headers=_hdr(tenant="tenant-iso-b"))
    assert rb.status_code == 200
    ids_b = {x["source_resource_id"] for x in rb.json()["results"]}
    assert "outline/team/doc_iso_b" in ids_b
    assert "outline/team/doc_iso_a" not in ids_b


def test_stepwise_sync_then_search(client):
    r = client.post("/v1/knowledge/sync", json={"tenant_id": "tenant-a", "documents": [{"resource_id": "outline/team/doc_step", "title": "Step Doc", "content": "stepwise retrieval pipeline test hello", "acl": {}}]}, headers=_hdr())
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["fetched"] == 1
    assert j["persisted"] >= 1
    r2 = client.post("/v1/knowledge/search", json={"query": "stepwise", "limit": 5}, headers=_hdr())
    assert r2.status_code == 200
    results = r2.json()["results"]
    assert any("stepwise" in x["chunk_text"] for x in results)
    # provenance preserved
    hit = next(x for x in results if "stepwise" in x["chunk_text"])
    assert hit["provenance"] is not None
    assert hit["source_system"] == "outline"


def test_acl_prefilter_groups(client):
    r = client.post("/v1/knowledge/sync", json={"tenant_id": "tenant-a", "documents": [{"resource_id": "outline/team/doc_acl_fin", "title": "Fin", "content": "finance secret budget 2026", "acl": {"groups": ["finance"]}}]}, headers=_hdr())
    assert r.status_code == 200
    # without finance group -> not visible
    r1 = client.post("/v1/knowledge/search", json={"query": "finance secret", "limit": 10}, headers=_hdr(groups=""))
    assert r1.status_code == 200
    assert all("finance secret" not in x["chunk_text"] for x in r1.json()["results"]) or not any(x["source_resource_id"] == "outline/team/doc_acl_fin" for x in r1.json()["results"])
    # with finance group -> visible
    r2 = client.post("/v1/knowledge/search", json={"query": "finance secret", "limit": 10}, headers=_hdr(groups="finance"))
    assert r2.status_code == 200
    ids = {x["source_resource_id"] for x in r2.json()["results"]}
    assert "outline/team/doc_acl_fin" in ids


def test_sync_fail_closed_no_creds(client, monkeypatch=None):
    # without injected docs and without env creds -> 503
    for k in ("OUTLINE_API_URL", "OUTLINE_API_KEY", "OUTLINE_API_TOKEN", "OAOS_OUTLINE_TOKEN", "OAOS_OUTLINE_URL"):
        os.environ.pop(k, None)
    r = client.post("/v1/knowledge/sync", json={"tenant_id": "tenant-a"}, headers=_hdr())
    assert r.status_code == 503
    assert "Outline credentials missing" in r.json().get("detail", "")


def test_materialize_gated_writes(client):
    # without write_enabled -> 403 or 503 (missing creds also)
    for k in ("OUTLINE_API_URL", "OUTLINE_API_KEY", "OUTLINE_API_TOKEN", "OAOS_OUTLINE_TOKEN"):
        os.environ.pop(k, None)
    r = client.post("/v1/knowledge/materialize", json={"title": "T", "text": "hello"}, headers=_hdr())
    # missing creds -> 503, not 403
    assert r.status_code == 503
    # with creds placeholder but without write_enabled -> 403
    # Use fake url/token via payload, but omit write_enabled
    r2 = client.post("/v1/knowledge/materialize", json={"title": "T2", "text": "hello2", "api_url": "https://o.example.com", "api_token": "tok"}, headers=_hdr())
    assert r2.status_code == 403
    assert "writes disabled" in r2.json().get("detail", "")
