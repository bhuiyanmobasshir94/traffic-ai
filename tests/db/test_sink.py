"""`RepositorySink` / `RepositoryHistory` — the adapters between `Database` and its consumers.

The delegation tests run against a fake `Database` and a fake `CrossingRepository`, so
they need no Postgres: what they pin down is the session discipline (one session per
call, always closed, never shared) and that errors reach the caller untouched — the
writer and the history routes each decide what a database failure means, and an
adapter that swallowed one would hide it from them.

The round-trip at the bottom needs a real Postgres and is skipped without
`TRAFFIC_AI_TEST_DATABASE_URL`, like `tests/db/test_repository.py`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from traffic_ai.db import sink as sink_module
from traffic_ai.db.models import Base
from traffic_ai.db.session import Database
from traffic_ai.db.sink import RepositoryHistory, RepositorySink
from traffic_ai.db.writer import CrossingWriter
from traffic_ai.domain import CrossingEvent, Direction

T0 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
_URL_ENV = "TRAFFIC_AI_TEST_DATABASE_URL"


def _event(n: int, *, at: datetime = T0, camera_id: str = "toll-plaza-a") -> CrossingEvent:
    return CrossingEvent(
        camera_id=camera_id,
        track_id=n,
        vehicle_class="car",
        direction=Direction.INCOMING,
        crossed_at=at,
        confidence=0.9,
    )


class _FakeSession:
    def __init__(self, number: int) -> None:
        self.number = number
        self.open = True


class _FakeDatabase:
    """Hands out numbered sessions and records when each one is closed."""

    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []

    @asynccontextmanager
    async def session(self) -> AsyncIterator[_FakeSession]:
        session = _FakeSession(len(self.sessions) + 1)
        self.sessions.append(session)
        try:
            yield session
        finally:
            session.open = False


class _FakeRepository:
    """Stands in for `CrossingRepository`; records the session it was built over."""

    instances: list[_FakeRepository]
    error: Exception | None = None
    fixed_events: list[CrossingEvent]

    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.calls: list[tuple[Any, ...]] = []
        type(self).instances.append(self)

    async def add_many(self, events: Sequence[CrossingEvent]) -> int:
        self.calls.append(("add_many", list(events)))
        assert self.session.open, "repository used after its session closed"
        if type(self).error is not None:
            raise type(self).error
        return len(events)

    async def recent(self, camera_id: str | None, limit: int) -> list[CrossingEvent]:
        self.calls.append(("recent", camera_id, limit))
        return [_event(1)]

    async def counts_by_class(
        self, camera_id: str | None, since: datetime, until: datetime
    ) -> dict[str, dict[str, int]]:
        self.calls.append(("counts_by_class", camera_id, since, until))
        return {"incoming": {"car": 2}}

    async def hourly_totals(
        self, camera_id: str | None, since: datetime, until: datetime
    ) -> list[tuple[datetime, int]]:
        self.calls.append(("hourly_totals", camera_id, since, until))
        return [(T0, 2)]


@pytest.fixture
def fake_repository(monkeypatch) -> type[_FakeRepository]:
    # A fresh subclass per test, so recorded instances and a configured error cannot
    # leak from one test into the next.
    class _Repo(_FakeRepository):
        instances: list[_FakeRepository] = []  # noqa: RUF012 - per-test class attribute
        error: Exception | None = None

    monkeypatch.setattr(sink_module, "CrossingRepository", _Repo)
    return _Repo


# --- RepositorySink ----------------------------------------------------------


async def test_add_many_commits_through_the_repository_and_returns_its_count(
    fake_repository,
) -> None:
    db = _FakeDatabase()
    events = [_event(1), _event(2), _event(3)]

    written = await RepositorySink(db).add_many(events)  # type: ignore[arg-type]

    assert written == 3
    (repo,) = fake_repository.instances
    assert repo.calls == [("add_many", events)]
    assert repo.session is db.sessions[0]


async def test_each_add_many_call_gets_its_own_session_and_closes_it(fake_repository) -> None:
    db = _FakeDatabase()
    sink = RepositorySink(db)  # type: ignore[arg-type]

    await sink.add_many([_event(1)])
    await sink.add_many([_event(2)])

    assert [s.number for s in db.sessions] == [1, 2]
    assert all(not s.open for s in db.sessions)
    assert [r.session.number for r in fake_repository.instances] == [1, 2]


async def test_a_failing_write_propagates_and_still_closes_the_session(fake_repository) -> None:
    """The writer is what absorbs a flush failure; the adapter must not."""
    fake_repository.error = ConnectionError("connection refused")
    db = _FakeDatabase()

    with pytest.raises(ConnectionError, match="connection refused"):
        await RepositorySink(db).add_many([_event(1)])  # type: ignore[arg-type]

    assert db.sessions[0].open is False


async def test_the_writer_flushes_through_the_sink_end_to_end(fake_repository) -> None:
    db = _FakeDatabase()
    writer = CrossingWriter(
        RepositorySink(db),  # type: ignore[arg-type]
        flush_interval_seconds=60.0,
        flush_max_batch=2,
    )
    task = asyncio.create_task(writer.run())

    for n in range(5):
        writer.submit(_event(n))
    writer.request_stop()
    await asyncio.wait_for(task, timeout=2.0)

    delivered = [
        e.track_id for repo in fake_repository.instances for call in repo.calls for e in call[1]
    ]
    assert sorted(delivered) == [0, 1, 2, 3, 4]
    assert writer.pending_count == 0
    # Two events per batch -> three flushes, three sessions, none left open.
    assert len(db.sessions) == 3
    assert all(not s.open for s in db.sessions)


async def test_the_writer_survives_a_database_that_fails_every_flush(fake_repository) -> None:
    fake_repository.error = ConnectionError("db down")
    db = _FakeDatabase()
    writer = CrossingWriter(
        RepositorySink(db),  # type: ignore[arg-type]
        flush_interval_seconds=60.0,
        flush_max_batch=100,
    )
    task = asyncio.create_task(writer.run())

    writer.submit(_event(1))
    writer.request_stop()
    await asyncio.wait_for(task, timeout=2.0)  # returns, rather than raising out of run()

    assert task.exception() is None
    assert all(not s.open for s in db.sessions)


# --- RepositoryHistory -------------------------------------------------------


async def test_history_methods_delegate_with_their_arguments_unchanged(fake_repository) -> None:
    db = _FakeDatabase()
    history = RepositoryHistory(db)  # type: ignore[arg-type]
    since, until = T0, T0 + timedelta(hours=6)

    assert await history.recent("toll-plaza-a", 25) == [_event(1)]
    assert await history.counts_by_class(None, since, until) == {"incoming": {"car": 2}}
    assert await history.hourly_totals("toll-plaza-b", since, until) == [(T0, 2)]

    calls = [r.calls[0] for r in fake_repository.instances]
    assert calls == [
        ("recent", "toll-plaza-a", 25),
        ("counts_by_class", None, since, until),
        ("hourly_totals", "toll-plaza-b", since, until),
    ]


async def test_history_opens_one_session_per_query_and_closes_it(fake_repository) -> None:
    """Concurrent requests must not share a session: `AsyncSession` is not safe for
    concurrent use, and one slow query would hold the connection for the rest."""
    db = _FakeDatabase()
    history = RepositoryHistory(db)  # type: ignore[arg-type]

    await asyncio.gather(
        history.recent(None, 1),
        history.recent(None, 2),
        history.counts_by_class(None, T0, T0 + timedelta(hours=1)),
    )

    assert len(db.sessions) == 3
    assert len({id(s) for s in db.sessions}) == 3
    assert all(not s.open for s in db.sessions)


async def test_history_query_errors_propagate_to_the_route_layer(
    fake_repository, monkeypatch
) -> None:
    class _Boom(fake_repository):
        async def recent(self, camera_id, limit):
            raise TimeoutError("pool exhausted")

    monkeypatch.setattr(sink_module, "CrossingRepository", _Boom)

    db = _FakeDatabase()
    with pytest.raises(TimeoutError, match="pool exhausted"):
        await RepositoryHistory(db).recent(None, 5)  # type: ignore[arg-type]
    assert db.sessions[0].open is False


# --- real Postgres -----------------------------------------------------------


@pytest.fixture
async def real_db() -> AsyncIterator[Database]:
    # NullPool: each test gets its own event loop, and a pooled connection must not
    # outlive the loop that opened it.
    engine = create_async_engine(os.environ[_URL_ENV], poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    database = Database(engine)
    yield database
    await database.close()


@pytest.mark.requires_postgres
@pytest.mark.skipif(not os.environ.get(_URL_ENV), reason=f"{_URL_ENV} is not set")
async def test_sink_writes_are_visible_to_history_reads(real_db: Database) -> None:
    sink, history = RepositorySink(real_db), RepositoryHistory(real_db)
    events = [
        _event(1, at=T0),
        _event(2, at=T0 + timedelta(minutes=30)),
        _event(3, at=T0 + timedelta(hours=2), camera_id="toll-plaza-b"),
    ]

    assert await sink.add_many(events) == 3

    recent = await history.recent(None, 10)
    assert [e.track_id for e in recent] == [3, 2, 1]
    assert [e.track_id for e in await history.recent("toll-plaza-a", 10)] == [2, 1]
    assert await history.counts_by_class(None, T0, T0 + timedelta(hours=3)) == {
        "incoming": {"car": 3}
    }
    hourly = await history.hourly_totals(None, T0, T0 + timedelta(hours=3))
    assert hourly == [(T0, 2), (T0 + timedelta(hours=2), 1)]  # 11:00 absent, not zero
