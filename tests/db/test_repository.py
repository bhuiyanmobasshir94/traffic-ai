"""`CrossingRepository` against a real Postgres.

The interesting behaviour here is SQL — GROUP BY, `date_trunc`, ON CONFLICT — and
SQLite would happily run a query Postgres rejects, so these only mean something on
the real engine. They are marked `requires_postgres` and skipped unless
`TRAFFIC_AI_TEST_DATABASE_URL` points at a database the suite may freely wipe: every
test drops and recreates the schema.

The skip lives in this module because it cannot live in `tests/conftest.py`'s
collection hook — that file is shared and owned elsewhere.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from traffic_ai.db.models import Base, CameraCountSnapshotRow, CrossingEventRow
from traffic_ai.db.repository import CameraCountSnapshot, CrossingRepository
from traffic_ai.db.session import Database
from traffic_ai.domain import CongestionLevel, CrossingEvent, Direction

_URL_ENV = "TRAFFIC_AI_TEST_DATABASE_URL"

pytestmark = [
    pytest.mark.requires_postgres,
    pytest.mark.skipif(not os.environ.get(_URL_ENV), reason=f"{_URL_ENV} is not set"),
]

T0 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)


def _event(
    *,
    at: datetime = T0,
    camera_id: str = "toll-plaza-a",
    track_id: int = 1,
    vehicle_class: str = "car",
    direction: Direction = Direction.INCOMING,
    plate_text: str | None = None,
    plate_confidence: float | None = None,
) -> CrossingEvent:
    return CrossingEvent(
        camera_id=camera_id,
        track_id=track_id,
        vehicle_class=vehicle_class,
        direction=direction,
        crossed_at=at,
        confidence=0.8,
        plate_text=plate_text,
        plate_confidence=plate_confidence,
    )


def _snapshot(*, count: int = 5, at: datetime = T0, camera_id: str = "toll-plaza-a"):
    return CameraCountSnapshot(
        camera_id=camera_id,
        captured_at=at,
        direction=Direction.INCOMING,
        vehicle_class="car",
        count=count,
        congestion=CongestionLevel.MODERATE,
        throughput_per_min=12.5,
    )


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    # NullPool: each test gets its own event loop, and a pooled connection must not
    # outlive the loop that opened it.
    engine = create_async_engine(os.environ[_URL_ENV], poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    database = Database(engine)
    yield database
    await database.close()


async def test_ping_reports_a_reachable_database(db: Database) -> None:
    assert await db.ping() is True


async def test_add_many_returns_the_number_of_rows_written(db: Database) -> None:
    async with db.session() as session:
        written = await CrossingRepository(session).add_many(
            [_event(track_id=1), _event(track_id=2)]
        )

    assert written == 2


async def test_add_many_with_nothing_writes_nothing(db: Database) -> None:
    async with db.session() as session:
        assert await CrossingRepository(session).add_many([]) == 0
        assert await session.scalar(select(func.count()).select_from(CrossingEventRow)) == 0


async def test_an_event_round_trips_with_a_timezone_aware_timestamp(db: Database) -> None:
    sent = _event(track_id=7, vehicle_class="bus", direction=Direction.OUTGOING)

    async with db.session() as session:
        repo = CrossingRepository(session)
        await repo.add_many([sent])
        (got,) = await repo.recent("toll-plaza-a", 10)

    assert got == sent
    assert got.crossed_at.tzinfo is not None


async def test_plate_fields_stay_null_unless_supplied(db: Database) -> None:
    async with db.session() as session:
        repo = CrossingRepository(session)
        await repo.add_many([_event(track_id=1)])
        (got,) = await repo.recent(None, 10)
        raw = (
            await session.execute(text("SELECT plate_text, plate_confidence FROM crossing_events"))
        ).one()

    assert got.plate_text is None
    assert got.plate_confidence is None
    assert tuple(raw) == (None, None)


async def test_a_supplied_plate_is_stored_verbatim(db: Database) -> None:
    async with db.session() as session:
        repo = CrossingRepository(session)
        await repo.add_many([_event(plate_text="DHAKA-1234", plate_confidence=0.71)])
        (got,) = await repo.recent(None, 10)

    assert got.plate_text == "DHAKA-1234"
    assert got.plate_confidence == pytest.approx(0.71)


async def test_created_at_is_set_by_the_server(db: Database) -> None:
    async with db.session() as session:
        await CrossingRepository(session).add_many([_event()])
        created_at = await session.scalar(select(CrossingEventRow.created_at))

    assert created_at is not None
    assert created_at.tzinfo is not None


async def test_recent_is_newest_first(db: Database) -> None:
    events = [_event(track_id=n, at=T0 + timedelta(minutes=n)) for n in (2, 0, 3, 1)]

    async with db.session() as session:
        repo = CrossingRepository(session)
        await repo.add_many(events)  # inserted out of order on purpose
        got = await repo.recent("toll-plaza-a", 10)

    assert [e.track_id for e in got] == [3, 2, 1, 0]


async def test_recent_honours_the_limit(db: Database) -> None:
    async with db.session() as session:
        repo = CrossingRepository(session)
        await repo.add_many([_event(track_id=n, at=T0 + timedelta(minutes=n)) for n in range(5)])
        got = await repo.recent("toll-plaza-a", 2)

    assert [e.track_id for e in got] == [4, 3]


async def test_recent_filters_by_camera_and_merges_when_unfiltered(db: Database) -> None:
    async with db.session() as session:
        repo = CrossingRepository(session)
        await repo.add_many(
            [
                _event(camera_id="toll-plaza-a", track_id=1, at=T0),
                _event(camera_id="toll-plaza-b", track_id=2, at=T0 + timedelta(minutes=1)),
            ]
        )
        only_a = await repo.recent("toll-plaza-a", 10)
        merged = await repo.recent(None, 10)

    assert [e.track_id for e in only_a] == [1]
    assert [e.track_id for e in merged] == [2, 1]


async def test_recent_returns_pydantic_models_not_orm_rows(db: Database) -> None:
    async with db.session() as session:
        repo = CrossingRepository(session)
        await repo.add_many([_event()])
        (got,) = await repo.recent(None, 1)

    assert isinstance(got, CrossingEvent)
    assert isinstance(got.direction, Direction)


async def test_counts_by_class_groups_by_direction_then_class(db: Database) -> None:
    events = [
        _event(track_id=1, direction=Direction.INCOMING, vehicle_class="car"),
        _event(track_id=2, direction=Direction.INCOMING, vehicle_class="car"),
        _event(track_id=3, direction=Direction.INCOMING, vehicle_class="bus"),
        _event(track_id=4, direction=Direction.OUTGOING, vehicle_class="car"),
    ]

    async with db.session() as session:
        repo = CrossingRepository(session)
        await repo.add_many(events)
        counts = await repo.counts_by_class("toll-plaza-a", T0, T0 + timedelta(hours=1))

    assert counts == {"incoming": {"car": 2, "bus": 1}, "outgoing": {"car": 1}}


async def test_counts_by_class_window_is_half_open(db: Database) -> None:
    since, until = T0, T0 + timedelta(hours=1)
    events = [
        _event(track_id=1, at=since - timedelta(seconds=1)),  # before
        _event(track_id=2, at=since),  # included
        _event(track_id=3, at=until - timedelta(seconds=1)),  # included
        _event(track_id=4, at=until),  # excluded: belongs to the next window
    ]

    async with db.session() as session:
        repo = CrossingRepository(session)
        await repo.add_many(events)
        counts = await repo.counts_by_class("toll-plaza-a", since, until)

    assert counts == {"incoming": {"car": 2}}


async def test_counts_by_class_scopes_to_a_camera_or_spans_all(db: Database) -> None:
    async with db.session() as session:
        repo = CrossingRepository(session)
        await repo.add_many(
            [
                _event(camera_id="toll-plaza-a", track_id=1),
                _event(camera_id="toll-plaza-b", track_id=2),
                _event(camera_id="toll-plaza-b", track_id=3),
            ]
        )
        window = (T0, T0 + timedelta(hours=1))
        only_b = await repo.counts_by_class("toll-plaza-b", *window)
        everything = await repo.counts_by_class(None, *window)

    assert only_b == {"incoming": {"car": 2}}
    assert everything == {"incoming": {"car": 3}}


async def test_counts_by_class_is_empty_when_nothing_matches(db: Database) -> None:
    async with db.session() as session:
        counts = await CrossingRepository(session).counts_by_class(
            "toll-plaza-a", T0, T0 + timedelta(hours=1)
        )

    assert counts == {}


async def test_hourly_totals_bucket_by_hour_oldest_first(db: Database) -> None:
    events = [
        _event(track_id=1, at=T0 + timedelta(minutes=5)),
        _event(track_id=2, at=T0 + timedelta(minutes=55)),
        _event(track_id=3, at=T0 + timedelta(hours=2, minutes=10)),
        _event(track_id=4, at=T0 + timedelta(minutes=30)),
    ]

    async with db.session() as session:
        repo = CrossingRepository(session)
        await repo.add_many(events)
        totals = await repo.hourly_totals("toll-plaza-a", T0, T0 + timedelta(hours=6))

    assert totals == [(T0, 3), (T0 + timedelta(hours=2), 1)]  # the empty hour is absent


async def test_hourly_totals_window_and_camera_filter(db: Database) -> None:
    async with db.session() as session:
        repo = CrossingRepository(session)
        await repo.add_many(
            [
                _event(camera_id="toll-plaza-a", track_id=1, at=T0),
                _event(camera_id="toll-plaza-b", track_id=2, at=T0),
                _event(camera_id="toll-plaza-a", track_id=3, at=T0 + timedelta(hours=3)),
            ]
        )
        window = (T0, T0 + timedelta(hours=2))
        a_only = await repo.hourly_totals("toll-plaza-a", *window)
        both = await repo.hourly_totals(None, *window)

    assert a_only == [(T0, 1)]
    assert both == [(T0, 2)]


async def test_upsert_snapshots_inserts_a_new_bucket(db: Database) -> None:
    async with db.session() as session:
        written = await CrossingRepository(session).upsert_snapshots([_snapshot(count=5)])
        stored = await session.scalar(select(func.count()).select_from(CameraCountSnapshotRow))

    assert written == 1
    assert stored == 1


async def test_upsert_snapshots_is_idempotent_and_the_latest_write_wins(db: Database) -> None:
    async with db.session() as session:
        repo = CrossingRepository(session)
        await repo.upsert_snapshots([_snapshot(count=5)])
        await repo.upsert_snapshots([_snapshot(count=5)])  # an exact retry
        await repo.upsert_snapshots([_snapshot(count=9)])  # same bucket, newer number

        rows = (await session.execute(select(CameraCountSnapshotRow))).scalars().all()

    assert len(rows) == 1
    assert rows[0].count == 9
    assert rows[0].congestion == "moderate"


async def test_upsert_snapshots_keeps_distinct_buckets_separate(db: Database) -> None:
    async with db.session() as session:
        repo = CrossingRepository(session)
        await repo.upsert_snapshots(
            [
                _snapshot(at=T0),
                _snapshot(at=T0 + timedelta(minutes=5)),
                _snapshot(at=T0, camera_id="toll-plaza-b"),
            ]
        )
        stored = await session.scalar(select(func.count()).select_from(CameraCountSnapshotRow))

    assert stored == 3


async def test_upsert_snapshots_with_nothing_writes_nothing(db: Database) -> None:
    async with db.session() as session:
        assert await CrossingRepository(session).upsert_snapshots([]) == 0
