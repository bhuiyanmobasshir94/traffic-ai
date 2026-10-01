"""Async engine and session factory.

Shaped like `StateStore` in `store.py` — a constructor over an already-built
client, a classmethod that builds one from configuration, `ping()`, and `close()`
— so the worker's lifespan can own both storage backends the same way.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from traffic_ai.config import Settings


class Database:
    """Owns the async engine and the session factory for one process."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        # Objects stay readable after commit: the repository maps rows to Pydantic
        # models after the transaction ends, and a lazy refresh there would need a
        # second round-trip on a session that may already be closed.
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> Database:
        engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            echo=settings.db_echo,
            # A connection the server recycled (idle timeout, failover, restart) is
            # detected on checkout and replaced, rather than surfacing as a failed
            # query on whichever request happened to draw it.
            pool_pre_ping=True,
        )
        return cls(engine)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session:
            try:
                yield session
            except Exception:
                # Roll back explicitly so a half-applied transaction is never
                # returned to the pool holding locks.
                await session.rollback()
                raise

    async def ping(self) -> bool:
        """Liveness probe for the readiness endpoint. Never raises."""
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            # Same contract as `StateStore.ping`: a probe reports False, it does
            # not crash the caller. Readiness must survive Postgres being down.
            return False

    async def close(self) -> None:
        await self._engine.dispose()
