"""DB engine / session factory helpers — async SQLAlchemy 2.0 + asyncpg."""
from __future__ import annotations

import os
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL") or os.environ.get("OAOS_DATABASE_URL", "")
    if not url:
        url = "postgresql+asyncpg://openagentos:secret@localhost:5432/openagentos"
    # ensure async driver
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def get_engine(url: str | None = None) -> AsyncEngine:
    return create_async_engine(url or _database_url(), echo=False, pool_pre_ping=True)


def get_sessionmaker(engine: AsyncEngine | None = None) -> async_sessionmaker[AsyncSession]:
    eng = engine or get_engine()
    return async_sessionmaker(eng, expire_on_commit=False, class_=AsyncSession)


async def get_async_session():
    """FastAPI dependency — yields AsyncSession."""
    maker = get_sessionmaker()
    async with maker() as session:
        yield session
