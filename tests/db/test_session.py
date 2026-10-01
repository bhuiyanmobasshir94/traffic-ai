"""`Database` — the readiness probe, and the engine settings that keep row data out of errors.

SQLite (aiosqlite, already a test dependency) stands in for Postgres here. What is under
test is not SQL dialect behaviour but two things that hold on any backend: that
`ping()` is False for a reachable server with no schema, and that SQLAlchemy was told to
hide bound parameters. The real-Postgres behaviour of the same probe is covered where a
database is available (`tests/db/test_repository.py`, `requires_postgres`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from traffic_ai.config import Settings
from traffic_ai.db.models import Base, CrossingEventRow
from traffic_ai.db.session import Database


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'history.db'}"


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(_sqlite_url(tmp_path))
    yield engine
    await engine.dispose()


# --- ping: connected is not ready --------------------------------------------------


async def test_ping_is_false_for_a_reachable_database_with_no_schema(engine: AsyncEngine) -> None:
    """The case this exists for: a fresh, unmigrated database answers `SELECT 1` while every
    flush and history query fails on the missing table. Readiness must not call that healthy."""
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1  # connectivity is fine

    assert await Database(engine).ping() is False


async def test_ping_is_true_once_the_schema_exists(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    assert await Database(engine).ping() is True


async def test_ping_is_false_when_the_table_is_missing_a_column_the_writer_inserts(
    engine: AsyncEngine,
) -> None:
    """A stale schema (an older migration) fails every insert just as a missing table does."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE crossing_events (id INTEGER PRIMARY KEY)"))

    assert await Database(engine).ping() is False


async def test_ping_reads_no_rows_and_leaves_the_table_alone(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    database = Database(engine)

    assert await database.ping() is True
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT COUNT(*) FROM crossing_events"))).scalar_one() == 0


async def test_ping_is_false_and_does_not_raise_for_an_unreachable_database(
    tmp_path: Path,
) -> None:
    unreachable = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'no-such-dir' / 'x.db'}")
    try:
        assert await Database(unreachable).ping() is False
    finally:
        await unreachable.dispose()


# --- bound parameters never reach an error message ------------------------------------


def test_the_engine_is_built_with_hidden_parameters() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://traffic:not-a-real-password@db:5432/traffic_ai",
    )
    database = Database.from_settings(settings)

    assert database._engine.sync_engine.hide_parameters is True


async def test_a_failing_statement_does_not_echo_its_parameters(tmp_path: Path) -> None:
    """The observable effect of `hide_parameters`: the error SQLAlchemy raises for a failed
    INSERT would otherwise end `[parameters: (...)]` -- the crossing row, and plate text once
    a model exists -- into every traceback and log line that carries it."""
    settings = Settings(_env_file=None, database_url=_sqlite_url(tmp_path))  # type: ignore[call-arg]
    database = Database.from_settings(settings)
    try:
        async with database._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        with pytest.raises(IntegrityError) as caught:
            async with database.session() as session:
                # `track_id` and the other NOT NULL columns are missing, so this fails.
                await session.execute(insert(CrossingEventRow).values(camera_id="SECRET-CAMERA"))

        assert "SECRET-CAMERA" not in str(caught.value)
        assert "SECRET-CAMERA" not in repr(caught.value)
    finally:
        await database.close()
