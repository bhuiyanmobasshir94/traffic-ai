"""Async engine and session factory.

Shaped like `StateStore` in `store.py` — a constructor over an already-built
client, a classmethod that builds one from configuration, `ping()`, and `close()`
— so the worker's lifespan can own both storage backends the same way.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from traffic_ai.config import Settings
from traffic_ai.db.models import CrossingEventRow


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
            # Bound parameters are crossing rows -- and plate text, once a plate model
            # exists. Without this every `DBAPIError` message, and so every traceback
            # and log line that carries one, embeds them.
            hide_parameters=True,
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
        """Readiness probe: True only when history can actually be written and read.

        Connectivity alone is not enough. A freshly provisioned Postgres that has not
        been migrated answers `SELECT 1` happily while every flush and every history
        query fails on the missing table -- the readiness endpoint would report a
        healthy database while all history is being lost. So the probe selects the
        mapped columns of `crossing_events` with `LIMIT 0`: it fails when the table or
        a column is absent, and costs one round trip and no row reads when it is not.

        Never raises. The caller bounds the wait (`routes._DATABASE_PROBE_TIMEOUT_SECONDS`).
        """
        try:
            async with self._engine.connect() as conn:
                await conn.execute(select(CrossingEventRow).limit(0))
            return True
        except Exception:
            # Same contract as `StateStore.ping`: a probe reports False, it does
            # not crash the caller. Readiness must survive Postgres being down.
            return False

    async def close(self) -> None:
        await self._engine.dispose()
