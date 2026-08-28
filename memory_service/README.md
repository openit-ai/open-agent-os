# Memory Service — Open Agent OS v1.6 §27

Persistent Memory service for Open Agent OS.

- **Source of Truth**: PostgreSQL `openagentos` database + `pgvector` extension
- **Runtime Independence**: LLM/Hermes runtimes access memory only via this service (no direct DB)
- **Isolation**: tenant + ACL-filtered retrieval (allowed namespaces before search)

## Tables (see `security/models/orm.py`)

- `memories` — id, tenant_id, user_id, agent_id, kind, content, embedding VECTOR(1536) nullable, source_ids JSON, created_at, updated_at
- `memory_sources` — provenance: id, tenant_id, memory_id FK, source_type, source_id, source_uri, metadata JSON, created_at
- `admin_state` — Admin Console persistent KV: key PK, value JSON, category, updated_at, updated_by

Embedding uses `pgvector.sqlalchemy.Vector(1536)` on Postgres; falls back to `Text` on SQLite/tests for `pytest` compatibility.

## Run

```bash
uvicorn memory-service.app:app --host 0.0.0.0 --port 8004 --reload
# or
python -m memory_service.app
```

## Health

```
GET /health → {"status":"ok","service":"memory-service"}
GET /v1/memory/health → same
```

## Roadmap

- `POST /v1/memories` — write with classification/provenance/retention
- `POST /v1/memories/search` — tenant/ACL-filtered semantic + structured search (pgvector)
- `POST /v1/memories/{id}/invalidate` — provenance revoke cascade
- Alembic migration: `alembic/versions/002_persistent_memory.py`
