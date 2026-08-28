"""Memory Service — FastAPI skeleton for v1.6 §27 Persistent Memory.

Runtime Independence:
  LLM/Hermes runtimes never connect to Postgres directly; they use this service.

Tables are defined in security/models/orm.py (MemoryORM, MemorySourceORM, AdminStateORM)
and created via alembic migration 002_persistent_memory.

For pytest/sqlite compatibility, embedding is pgvector Vector(1536) on Postgres
and Text fallback on SQLite (see security/models/orm.py).
"""
from __future__ import annotations

import os
from fastapi import FastAPI

app = FastAPI(title="Open Agent OS — Memory Service", version="0.1.1")


@app.get("/health")
def health():
    return {"status": "ok", "service": "memory-service"}


# Alias for control-plane style health checks
@app.get("/v1/memory/health")
def memory_health():
    return {"status": "ok", "service": "memory-service"}


@app.get("/")
def root():
    return {"service": "memory-service", "version": "0.1.1", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MEMORY_SERVICE_PORT", "8004"))
    uvicorn.run(app, host="0.0.0.0", port=port)
