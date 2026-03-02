"""Async SQLAlchemy engine and session factory.

TODO (Phase 3): Add ORM models mirroring schema.sql
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.db_url,
    pool_size=_settings.db_pool_size,
    echo=_settings.app_env == "development",
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:  # FastAPI dependency
    async with AsyncSessionLocal() as session:
        yield session
