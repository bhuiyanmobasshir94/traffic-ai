"""`/api/history/*` — the Postgres-backed history routes, and readiness's `database` field.

No database anywhere: the routes read through `get_history_reader`, which these tests
replace with a fake via FastAPI's dependency override. What is under test is the
route layer's own contract — validation order, the camera allowlist, the 503 policy,
and the response shapes — not SQL (that is `tests/db/test_repository.py`).

The 503 policy is the one that matters: a missing or failing database is answered with
a 503 and a reason, never an empty list or a block of zeros that a dashboard would
render as "no traffic".
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from traffic_ai.api import app as app_module
from traffic_ai.api import routes as routes_module
from traffic_ai.api.dependencies import MAX_HISTORY_WINDOW, get_history_reader
from traffic_ai.cameras import CAMERAS
from traffic_ai.db.models import Base
from traffic_ai.db.session import Database
from traffic_ai.db.writer import CrossingWriter
from traffic_ai.domain import CameraState, CrossingEvent, Direction, PipelineStatus

CAMERA = CAMERAS[0].camera_id
TOKEN = "correct-horse-battery-staple-0123456789"  # noqa: S105 - a test fixture, not a credential

T0 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)


def _event(track_id: int, *, at: datetime = T0, camera_id: str = CAMERA) -> CrossingEvent:
    return CrossingEvent(
        camera_id=camera_id,
        track_id=track_id,
        vehicle_class="car",
        direction=Direction.INCOMING,
        crossed_at=at,
        confidence=0.9,
    )


class _FakeHistory:
    """Stands in for `RepositoryHistory`. Records every call so tests can assert on
    exactly what reached the data layer — including that nothing did."""

    def __init__(
        self,
        *,
        events: Sequence[CrossingEvent] = (),
        counts: dict[str, dict[str, int]] | None = None,
        hourly: Sequence[tuple[datetime, int]] = (),
        error: Exception | None = None,
        hang: bool = False,
    ) -> None:
        self._events = list(events)
        self._counts = counts or {}
        self._hourly = list(hourly)
        self._error = error
        self._hang = hang
        self.calls: list[tuple[Any, ...]] = []

    async def _maybe_fail(self) -> None:
        if self._hang:
            await asyncio.Event().wait()  # never set: cancelled by the route's timeout
        if self._error is not None:
            raise self._error

    async def recent(self, camera_id: str | None, limit: int) -> list[CrossingEvent]:
        self.calls.append(("recent", camera_id, limit))
        await self._maybe_fail()
        return self._events

    async def counts_by_class(
        self, camera_id: str | None, since: datetime, until: datetime
    ) -> dict[str, dict[str, int]]:
        self.calls.append(("counts", camera_id, since, until))
        await self._maybe_fail()
        return self._counts

    async def hourly_totals(
        self, camera_id: str | None, since: datetime, until: datetime
    ) -> list[tuple[datetime, int]]:
        self.calls.append(("hourly", camera_id, since, until))
        await self._maybe_fail()
        return self._hourly


@pytest.fixture
def history_app(app_factory) -> Callable[..., Any]:
    """An app whose history routes read from `fake`. Persistence settings are left
    off (the default), so the override alone decides what the routes see."""

    def build(fake: _FakeHistory):
        app = app_factory()
        app.dependency_overrides[get_history_reader] = lambda: fake
        return app

    return build


ENDPOINTS = ["/api/history/events", "/api/history/counts", "/api/history/hourly"]


# --- 503: no database, never fabricated data ---------------------------------


@pytest.mark.parametrize("path", ENDPOINTS)
async def test_503_when_persistence_is_disabled(app_factory, running_app, path: str) -> None:
    app = app_factory()  # persistence off by default in the fixture
    async with running_app(app) as client:
        resp = await client.get(path)
    assert resp.status_code == 503
    assert resp.json() == {"detail": "history is unavailable: persistence is disabled"}


@pytest.mark.parametrize("path", ENDPOINTS)
async def test_503_when_the_database_failed_to_initialise(
    app_factory, running_app, monkeypatch, path: str
) -> None:
    """Persistence is on but `Database.from_settings` blew up at startup: the app
    stays up live-only, and history says so rather than answering with nothing."""

    def boom(settings):
        raise RuntimeError("no module named asyncpg")

    monkeypatch.setattr(app_module.Database, "from_settings", boom)
    app = app_factory(persistence=True)
    async with running_app(app) as client:
        resp = await client.get(path)
        live = await client.get("/api/healthz")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "history is unavailable: the database was not initialised"}
    assert live.status_code == 200


@pytest.mark.parametrize("path", ENDPOINTS)
async def test_503_when_the_query_raises_and_the_cause_is_not_echoed(
    history_app, running_app, path: str
) -> None:
    """A driver error can carry a DSN or a hostname. The client gets "query failed";
    the cause goes to the log."""
    app = history_app(_FakeHistory(error=RuntimeError("password authentication failed for user")))
    async with running_app(app) as client:
        resp = await client.get(path)
    assert resp.status_code == 503
    assert resp.json() == {"detail": "history is unavailable: the database query failed"}
    assert "password" not in resp.text


@pytest.mark.parametrize("path", ENDPOINTS)
async def test_503_when_the_query_hangs(history_app, running_app, monkeypatch, path: str) -> None:
    monkeypatch.setattr(routes_module, "_HISTORY_QUERY_TIMEOUT_SECONDS", 0.05)
    app = history_app(_FakeHistory(hang=True))
    async with running_app(app) as client:
        resp = await client.get(path)
    assert resp.status_code == 503
    assert resp.json() == {"detail": "history is unavailable: the database did not respond in time"}


# --- 404: camera allowlist ---------------------------------------------------


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize(
    "camera_id",
    ["no-such-camera", "", "toll-plaza-a'; DROP TABLE crossing_events;--", "TOLL-PLAZA-A"],
)
async def test_unknown_camera_is_404_and_never_reaches_the_database(
    history_app, running_app, path: str, camera_id: str
) -> None:
    fake = _FakeHistory()
    app = history_app(fake)
    async with running_app(app) as client:
        resp = await client.get(path, params={"camera_id": camera_id})
    assert resp.status_code == 404
    assert fake.calls == []


@pytest.mark.parametrize("path", ENDPOINTS)
async def test_unknown_camera_is_404_even_with_persistence_off(
    app_factory, running_app, path: str
) -> None:
    """Validation is answered before availability: a caller with a typo'd camera
    should learn that, not be told the database is down."""
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get(path, params={"camera_id": "nope"})
    assert resp.status_code == 404


async def test_a_known_camera_reaches_the_database_as_the_registry_id(
    history_app, running_app
) -> None:
    fake = _FakeHistory()
    app = history_app(fake)
    async with running_app(app) as client:
        resp = await client.get("/api/history/events", params={"camera_id": CAMERA})
    assert resp.status_code == 200
    assert fake.calls == [("recent", CAMERA, 100)]


# --- /history/events ---------------------------------------------------------


async def test_events_returns_the_rows_in_the_order_the_repository_gave(
    history_app, running_app
) -> None:
    newest, older = _event(2, at=T0 + timedelta(minutes=5)), _event(1)
    app = history_app(_FakeHistory(events=[newest, older]))
    async with running_app(app) as client:
        resp = await client.get("/api/history/events")
    assert resp.status_code == 200
    assert [e["track_id"] for e in resp.json()] == [2, 1]
    assert set(resp.json()[0]) == {
        "camera_id",
        "track_id",
        "vehicle_class",
        "direction",
        "crossed_at",
        "confidence",
        "plate_text",
        "plate_confidence",
    }
    assert resp.json()[0]["plate_text"] is None


async def test_events_default_limit_is_100_and_no_camera_means_all(
    history_app, running_app
) -> None:
    fake = _FakeHistory()
    app = history_app(fake)
    async with running_app(app) as client:
        resp = await client.get("/api/history/events")
    assert resp.status_code == 200
    assert resp.json() == []  # a live database genuinely holding nothing is a real answer
    assert fake.calls == [("recent", None, 100)]


@pytest.mark.parametrize("limit", [1, 1000])
async def test_events_limit_accepts_its_bounds(history_app, running_app, limit: int) -> None:
    fake = _FakeHistory()
    app = history_app(fake)
    async with running_app(app) as client:
        resp = await client.get("/api/history/events", params={"limit": limit})
    assert resp.status_code == 200
    assert fake.calls == [("recent", None, limit)]


@pytest.mark.parametrize("limit", ["0", "-1", "1001", "abc"])
async def test_events_limit_outside_bounds_is_422(history_app, running_app, limit: str) -> None:
    fake = _FakeHistory()
    app = history_app(fake)
    async with running_app(app) as client:
        resp = await client.get("/api/history/events", params={"limit": limit})
    assert resp.status_code == 422
    assert fake.calls == []


async def test_invalid_limit_is_422_not_503_when_persistence_is_off(
    app_factory, running_app
) -> None:
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get("/api/history/events", params={"limit": 0})
    assert resp.status_code == 422


# --- /history/counts ---------------------------------------------------------


async def test_counts_shape_and_total(history_app, running_app) -> None:
    fake = _FakeHistory(
        counts={"incoming": {"car": 5, "bus": 1}, "outgoing": {"car": 2}},
    )
    app = history_app(fake)
    since, until = "2026-03-01T00:00:00Z", "2026-03-02T00:00:00Z"
    async with running_app(app) as client:
        resp = await client.get(
            "/api/history/counts",
            params={"camera_id": CAMERA, "since": since, "until": until},
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "camera_id": CAMERA,
        "since": "2026-03-01T00:00:00Z",
        "until": "2026-03-02T00:00:00Z",
        "counts": {"incoming": {"car": 5, "bus": 1}, "outgoing": {"car": 2}},
        "total": 8,
    }
    assert fake.calls == [
        (
            "counts",
            CAMERA,
            datetime(2026, 3, 1, tzinfo=UTC),
            datetime(2026, 3, 2, tzinfo=UTC),
        )
    ]


async def test_counts_always_carries_both_directions(history_app, running_app) -> None:
    """A direction with no crossings is present and empty, so a reader never has to
    guard a missing key. The total is still the real sum."""
    app = history_app(_FakeHistory(counts={"incoming": {"truck": 3}}))
    async with running_app(app) as client:
        resp = await client.get("/api/history/counts")
    body = resp.json()
    assert body["counts"] == {"incoming": {"truck": 3}, "outgoing": {}}
    assert body["total"] == 3
    assert body["camera_id"] is None


async def test_counts_with_no_crossings_is_a_real_zero_from_a_live_database(
    history_app, running_app
) -> None:
    app = history_app(_FakeHistory(counts={}))
    async with running_app(app) as client:
        resp = await client.get("/api/history/counts")
    assert resp.status_code == 200
    assert resp.json()["counts"] == {"incoming": {}, "outgoing": {}}
    assert resp.json()["total"] == 0


async def test_default_window_is_the_last_24_hours_ending_now(history_app, running_app) -> None:
    fake = _FakeHistory()
    app = history_app(fake)
    before = datetime.now(UTC)
    async with running_app(app) as client:
        resp = await client.get("/api/history/counts")
    after = datetime.now(UTC)
    assert resp.status_code == 200

    ((_, _, since, until),) = fake.calls
    assert before <= until <= after
    assert until - since == timedelta(hours=24)
    assert since.tzinfo is not None


async def test_only_since_given_runs_until_now(history_app, running_app) -> None:
    fake = _FakeHistory()
    app = history_app(fake)
    since = datetime.now(UTC) - timedelta(hours=3)
    async with running_app(app) as client:
        resp = await client.get("/api/history/counts", params={"since": since.isoformat()})
    assert resp.status_code == 200
    ((_, _, got_since, got_until),) = fake.calls
    assert got_since == since
    assert timedelta(hours=2, minutes=59) < got_until - got_since < timedelta(hours=3, minutes=1)


async def test_only_until_given_looks_back_24_hours(history_app, running_app) -> None:
    fake = _FakeHistory()
    app = history_app(fake)
    async with running_app(app) as client:
        resp = await client.get("/api/history/counts", params={"until": "2026-03-05T12:00:00Z"})
    assert resp.status_code == 200
    ((_, _, since, until),) = fake.calls
    assert until == datetime(2026, 3, 5, 12, tzinfo=UTC)
    assert since == datetime(2026, 3, 4, 12, tzinfo=UTC)


async def test_offset_bounds_are_normalised_to_utc(history_app, running_app) -> None:
    fake = _FakeHistory()
    app = history_app(fake)
    async with running_app(app) as client:
        resp = await client.get(
            "/api/history/counts",
            params={"since": "2026-03-01T10:00:00+06:00", "until": "2026-03-01T16:00:00+06:00"},
        )
    assert resp.status_code == 200
    assert resp.json()["since"] == "2026-03-01T04:00:00Z"
    assert resp.json()["until"] == "2026-03-01T10:00:00Z"
    ((_, _, since, until),) = fake.calls
    assert since.utcoffset() == timedelta(0)
    assert until.utcoffset() == timedelta(0)


# --- 422: windows ------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/history/counts", "/api/history/hourly"])
@pytest.mark.parametrize(
    ("params", "needle"),
    [
        # Naive datetimes are rejected, not assumed to be UTC: a client sending local
        # time would silently get a shifted window and plausible, wrong numbers.
        ({"since": "2026-03-01T10:00:00"}, "since must include a UTC offset"),
        ({"until": "2026-03-01T10:00:00"}, "until must include a UTC offset"),
        # Half-open window: an empty one is invalid, not "zero crossings".
        (
            {"since": "2026-03-01T10:00:00Z", "until": "2026-03-01T10:00:00Z"},
            "since must be earlier than until",
        ),
        (
            {"since": "2026-03-02T10:00:00Z", "until": "2026-03-01T10:00:00Z"},
            "since must be earlier than until",
        ),
        # `since` in the future with the default `until` of now is also since >= until.
        ({"since": "2999-01-01T00:00:00Z"}, "since must be earlier than until"),
        (
            {"since": "2026-01-01T00:00:00Z", "until": "2026-03-01T00:00:00Z"},
            "window may not exceed 31 days",
        ),
        ({"since": "yesterday"}, None),
        ({"until": "not-a-date"}, None),
    ],
)
async def test_bad_windows_are_422_and_never_reach_the_database(
    history_app, running_app, path: str, params: dict[str, str], needle: str | None
) -> None:
    fake = _FakeHistory()
    app = history_app(fake)
    async with running_app(app) as client:
        resp = await client.get(path, params=params)
    assert resp.status_code == 422
    if needle is not None:
        assert needle in resp.json()["detail"]
    assert fake.calls == []


async def test_a_window_of_exactly_31_days_is_accepted(history_app, running_app) -> None:
    fake = _FakeHistory()
    app = history_app(fake)
    until = datetime(2026, 3, 31, tzinfo=UTC)
    async with running_app(app) as client:
        resp = await client.get(
            "/api/history/counts",
            params={"since": (until - MAX_HISTORY_WINDOW).isoformat(), "until": until.isoformat()},
        )
    assert resp.status_code == 200


async def test_a_window_one_second_over_31_days_is_422(history_app, running_app) -> None:
    app = history_app(_FakeHistory())
    until = datetime(2026, 3, 31, tzinfo=UTC)
    since = until - MAX_HISTORY_WINDOW - timedelta(seconds=1)
    async with running_app(app) as client:
        resp = await client.get(
            "/api/history/counts",
            params={"since": since.isoformat(), "until": until.isoformat()},
        )
    assert resp.status_code == 422


async def test_bad_window_is_422_not_503_when_persistence_is_off(app_factory, running_app) -> None:
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get(
            "/api/history/hourly",
            params={"since": "2026-03-02T00:00:00Z", "until": "2026-03-01T00:00:00Z"},
        )
    assert resp.status_code == 422


# --- /history/hourly ---------------------------------------------------------


async def test_hourly_shape(history_app, running_app) -> None:
    hours = [(T0, 4), (T0 + timedelta(hours=2), 9)]  # 11:00 is absent: not zero-filled
    fake = _FakeHistory(hourly=hours)
    app = history_app(fake)
    async with running_app(app) as client:
        resp = await client.get(
            "/api/history/hourly",
            params={
                "camera_id": CAMERA,
                "since": "2026-03-01T00:00:00Z",
                "until": "2026-03-02T00:00:00Z",
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "camera_id": CAMERA,
        "since": "2026-03-01T00:00:00Z",
        "until": "2026-03-02T00:00:00Z",
        "buckets": [
            {"hour": "2026-03-01T10:00:00Z", "total": 4},
            {"hour": "2026-03-01T12:00:00Z", "total": 9},
        ],
    }


async def test_hourly_with_no_rows_is_an_empty_bucket_list(history_app, running_app) -> None:
    app = history_app(_FakeHistory(hourly=[]))
    async with running_app(app) as client:
        resp = await client.get("/api/history/hourly")
    assert resp.status_code == 200
    body = resp.json()
    assert body["buckets"] == []
    assert body["camera_id"] is None


# --- auth ---------------------------------------------------------------------


@pytest.mark.parametrize("path", ENDPOINTS)
async def test_history_requires_the_api_token_when_one_is_configured(
    app_factory, running_app, settings_factory, path: str
) -> None:
    """History is not in `auth_exempt_paths`, so it sits behind the bearer token
    like every other data route — the middleware, not the route, enforces it."""
    fake = _FakeHistory()
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    app.dependency_overrides[get_history_reader] = lambda: fake
    async with running_app(app) as client:
        anonymous = await client.get(path)
        authed = await client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
    assert anonymous.status_code == 401
    assert authed.status_code == 200
    assert len(fake.calls) == 1  # only the authenticated request got through


# --- readiness: `database` is reported, never gating -------------------------


class _FakeDatabase:
    """Just the surface the app touches: `ping`, `close`, and (via `RepositorySink`) `session`."""

    def __init__(self, *, up: bool = True, hang: bool = False) -> None:
        self._up = up
        self._hang = hang
        self.closed = False

    async def ping(self) -> bool:
        if self._hang:
            await asyncio.Event().wait()
        return self._up

    async def close(self) -> None:
        self.closed = True


async def test_readyz_database_is_null_when_persistence_is_disabled(
    app_factory, running_app, make_pipeline
) -> None:
    app = app_factory(pipelines=[make_pipeline("toll-plaza-a")])
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.status_code == 200
    assert resp.json()["database"] is None


async def test_readyz_reports_a_reachable_database(app_factory, running_app, make_pipeline) -> None:
    app = app_factory(pipelines=[make_pipeline("toll-plaza-a")], database=_FakeDatabase(up=True))
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.status_code == 200
    assert resp.json()["database"] is True
    assert resp.json()["ready"] is True


async def test_readyz_stays_200_with_the_database_down(
    app_factory, running_app, make_pipeline
) -> None:
    """The live path does not depend on Postgres, so neither does readiness: pulling
    the instance out of rotation would take counts, frames and the stream down with
    a database that only history needs."""
    app = app_factory(pipelines=[make_pipeline("toll-plaza-a")], database=_FakeDatabase(up=False))
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["database"] is False
    assert body["detail"] is None


async def test_readyz_stays_200_when_the_database_probe_hangs(
    app_factory, running_app, make_pipeline, monkeypatch
) -> None:
    monkeypatch.setattr(routes_module, "_DATABASE_PROBE_TIMEOUT_SECONDS", 0.05)
    app = app_factory(pipelines=[make_pipeline("toll-plaza-a")], database=_FakeDatabase(hang=True))
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.status_code == 200
    assert resp.json()["database"] is False


async def test_readyz_stays_200_when_the_database_failed_to_initialise(
    app_factory, running_app, make_pipeline, monkeypatch
) -> None:
    def boom(settings):
        raise RuntimeError("bad url")

    monkeypatch.setattr(app_module.Database, "from_settings", boom)
    app = app_factory(pipelines=[make_pipeline("toll-plaza-a")], persistence=True)
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.status_code == 200
    assert resp.json()["database"] is False


async def test_readyz_is_still_503_for_non_database_reasons_and_reports_the_database(
    app_factory, running_app, make_pipeline
) -> None:
    app = app_factory(
        pipelines=[make_pipeline("toll-plaza-a", PipelineStatus.ERROR)],
        database=_FakeDatabase(up=True),
    )
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.status_code == 503
    assert resp.json()["database"] is True
    assert resp.json()["detail"] == "no camera pipeline running"


# --- readiness: a reachable database with no schema is not a healthy one ----------------


@pytest.fixture
async def sqlite_engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'history.db'}")
    yield engine
    await engine.dispose()


async def test_readyz_database_is_false_for_a_reachable_database_with_no_schema(
    app_factory, running_app, make_pipeline, sqlite_engine
) -> None:
    """Connectivity alone used to be enough: an unmigrated Postgres answered the ping, readiness
    said the database was fine, and every history write and query failed on the missing table."""
    app = app_factory(pipelines=[make_pipeline("toll-plaza-a")], database=Database(sqlite_engine))
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.json()["database"] is False
    assert resp.status_code == 200  # still reported, never gating


async def test_readyz_database_is_true_once_the_schema_exists(
    app_factory, running_app, make_pipeline, sqlite_engine
) -> None:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = app_factory(pipelines=[make_pipeline("toll-plaza-a")], database=Database(sqlite_engine))
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.json()["database"] is True


# --- readiness: history events lost ---------------------------------------------------


def _lossy_writer(*, submitted: int, buffer_limit: int) -> CrossingWriter:
    """A writer that never flushes, so only buffer evictions can lose events."""
    writer = CrossingWriter(
        _RecordingSink([]),
        flush_interval_seconds=60.0,
        flush_max_batch=100,
        buffer_limit=buffer_limit,
    )
    for n in range(submitted):
        writer.submit(_event(n))
    return writer


async def test_readyz_reports_how_many_history_events_were_lost(
    app_factory, running_app, make_pipeline
) -> None:
    writer = _lossy_writer(submitted=5, buffer_limit=2)  # 3 evicted
    app = app_factory(
        pipelines=[make_pipeline("toll-plaza-a")], database=_FakeDatabase(), writer=writer
    )
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.status_code == 200  # reported, never gating
    assert resp.json()["history_events_lost"] == 3


async def test_readyz_reports_zero_when_nothing_was_lost(
    app_factory, running_app, make_pipeline
) -> None:
    writer = _lossy_writer(submitted=2, buffer_limit=10)
    app = app_factory(
        pipelines=[make_pipeline("toll-plaza-a")], database=_FakeDatabase(), writer=writer
    )
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.json()["history_events_lost"] == 0


async def test_readyz_history_events_lost_is_null_when_persistence_is_disabled(
    app_factory, running_app, make_pipeline
) -> None:
    """Null is "there is no writer to ask", not "zero lost"."""
    app = app_factory(pipelines=[make_pipeline("toll-plaza-a")])
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.json()["history_events_lost"] is None


async def test_readyz_history_events_lost_is_null_when_the_database_failed_to_initialise(
    app_factory, running_app, make_pipeline, monkeypatch
) -> None:
    def boom(settings):
        raise RuntimeError("bad url")

    monkeypatch.setattr(app_module.Database, "from_settings", boom)
    app = app_factory(pipelines=[make_pipeline("toll-plaza-a")], persistence=True)
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.json()["history_events_lost"] is None


# --- database errors never put row data in a log line ---------------------------------


class _DriverError(Exception):
    pass


def _row_bearing_error() -> DBAPIError:
    return DBAPIError(
        "INSERT INTO crossing_events (plate_text) VALUES (%s)",
        ("SECRET-PLATE-0042",),
        _DriverError("DETAIL: Key (plate_text)=(SECRET-PLATE-0042) already exists."),
    )


@pytest.mark.parametrize("path", ENDPOINTS)
async def test_a_failed_history_query_logs_the_error_type_not_the_row_data(
    history_app, running_app, monkeypatch, path: str
) -> None:
    app = history_app(_FakeHistory(error=_row_bearing_error()))
    async with running_app(app) as client:
        with structlog.testing.capture_logs() as logs:
            # A fresh logger: `configure_logging` (run by the lifespan) caches the module's.
            monkeypatch.setattr(routes_module, "logger", structlog.get_logger("traffic_ai.api"))
            resp = await client.get(path)

    assert resp.status_code == 503
    (failed,) = [entry for entry in logs if entry["event"] == "history.query_failed"]
    assert failed["error_type"] == "DBAPIError"
    assert failed["error"] == "_DriverError"
    assert "SECRET-PLATE-0042" not in repr(logs)
    assert "SECRET-PLATE-0042" not in resp.text


async def test_a_database_init_failure_does_not_log_the_connection_url_credentials(
    app_factory, running_app, monkeypatch
) -> None:
    """SQLAlchemy quotes an unparseable URL, password and all, in the exception it raises."""

    def boom(settings):
        raise ValueError("Could not parse 'postgresql+asyncpg://traffic:hunter2@db:5432/x'")

    monkeypatch.setattr(app_module.Database, "from_settings", boom)
    monkeypatch.setattr(app_module, "configure_logging", lambda **_: None)
    with structlog.testing.capture_logs() as logs:
        monkeypatch.setattr(app_module, "logger", structlog.get_logger("traffic_ai.api.app"))
        app = app_factory(persistence=True)
        async with running_app(app):
            pass

    (failed,) = [entry for entry in logs if entry["event"] == "db.init_failed"]
    assert failed["error_type"] == "ValueError"
    assert "hunter2" not in repr(logs)
    assert "postgresql+asyncpg://" in failed["error"]  # still diagnosable


# --- L1: a window that cannot be represented in UTC is a 422, not a 500 ---------------


@pytest.mark.parametrize("path", ["/api/history/counts", "/api/history/hourly"])
@pytest.mark.parametrize(
    "params",
    [
        # Year 10000 once converted to UTC.
        {"until": "9999-12-31T23:59:59-05:00"},
        {"since": "9999-12-31T23:59:59-05:00"},
        # Year 0 once converted to UTC.
        {"since": "0001-01-01T00:00:00+05:00", "until": "2026-01-01T00:00:00Z"},
        # Fine on its own, but the default 24h window back from it leaves the range.
        {"until": "0001-01-01T00:00:00Z"},
    ],
)
async def test_an_unrepresentable_window_is_422_and_never_reaches_the_database(
    history_app, running_app, path: str, params: dict[str, str]
) -> None:
    fake = _FakeHistory()
    app = history_app(fake)
    async with running_app(app) as client:
        resp = await client.get(path, params=params)
    assert resp.status_code == 422
    assert "UTC" in resp.json()["detail"]
    assert fake.calls == []


# --- lifespan: wiring and shutdown order -------------------------------------


class _RecordingSink:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.delivered: list[int] = []

    async def add_many(self, events: Sequence[CrossingEvent]) -> int:
        self.delivered.extend(e.track_id for e in events)
        self._order.append(f"flushed:{[e.track_id for e in events]}")
        return len(events)


def _ordered_writer(order: list[str], sink: _RecordingSink) -> CrossingWriter:
    class _Writer(CrossingWriter):
        def request_stop(self) -> None:
            order.append("writer.request_stop")
            super().request_stop()

        async def run(self) -> None:
            await super().run()
            order.append("writer.drained")

    # A long interval, so nothing is flushed by the clock: every delivery in these
    # tests is the shutdown drain, which is exactly the behaviour being checked.
    return _Writer(sink, flush_interval_seconds=60.0, flush_max_batch=100)


class _WindingDownPipeline:
    """Counts one last crossing while shutting down, after `request_stop()`.

    A real pipeline can do exactly this: the stop flag is only checked between
    frames, so the tick in flight when shutdown begins still finishes and may count a
    crossing. That is the case the shutdown order exists for.
    """

    camera_id = CAMERA

    def __init__(self, order: list[str], writer: CrossingWriter | None) -> None:
        self._order = order
        self._writer = writer
        self._stop = asyncio.Event()

    @property
    def state(self):
        return CameraState(
            camera_id=CAMERA,
            name=CAMERA,
            status=PipelineStatus.RUNNING,
            updated_at=datetime.now(UTC),
        )

    async def run(self) -> None:
        await self._stop.wait()
        self._order.append("pipeline.stop_seen")
        await asyncio.sleep(0.05)  # winding down...
        if self._writer is not None:
            self._writer.submit(_event(99))  # ...and one last crossing is counted
        self._order.append("pipeline.returned")

    def request_stop(self) -> None:
        self._order.append("pipeline.request_stop")
        self._stop.set()


async def test_shutdown_stops_pipelines_before_the_writer_drains(app_factory, running_app) -> None:
    """If the writer were stopped first, its drain would already be over and the last
    crossing would sit in a buffer nobody flushes."""
    order: list[str] = []
    sink = _RecordingSink(order)
    writer = _ordered_writer(order, sink)
    database = _FakeDatabase()
    handed_writers: list[CrossingWriter | None] = []

    def factory(settings, store, writer=None):
        handed_writers.append(writer)
        return [_WindingDownPipeline(order, writer)]

    app = app_factory(database=database, writer=writer, pipeline_factory=factory)
    async with running_app(app) as client:
        assert (await client.get("/api/healthz")).status_code == 200

    assert handed_writers == [writer]
    # The last crossing, submitted during pipeline wind-down, was durably handed over.
    assert sink.delivered == [99]
    assert order == [
        "pipeline.request_stop",
        "pipeline.stop_seen",
        "pipeline.returned",
        "writer.request_stop",
        "flushed:[99]",
        "writer.drained",
    ]
    # An injected database belongs to the caller, like an injected store.
    assert database.closed is False


async def test_a_database_the_app_built_is_closed_only_after_the_writer_drains(
    app_factory, running_app, monkeypatch
) -> None:
    """The app owns a database it built itself, and closing it under a draining writer
    would fail the final flush — the history of the last few seconds."""
    order: list[str] = []

    class _OwnedDatabase(_FakeDatabase):
        async def close(self) -> None:
            order.append("database.close")
            await super().close()

    database = _OwnedDatabase()
    sink = _RecordingSink(order)
    monkeypatch.setattr(app_module.Database, "from_settings", lambda settings: database)
    monkeypatch.setattr(app_module, "RepositorySink", lambda db: sink)

    def factory(settings, store, writer=None):
        return [_WindingDownPipeline(order, writer)]

    app = app_factory(persistence=True, pipeline_factory=factory)
    async with running_app(app):
        assert app.state.database is database
        assert app.state.history is not None

    assert database.closed is True
    assert sink.delivered == [99]
    assert order == [
        "pipeline.request_stop",
        "pipeline.stop_seen",
        "pipeline.returned",
        "flushed:[99]",
        "database.close",
    ]


async def test_no_writer_is_built_or_offered_when_persistence_is_off(
    app_factory, running_app
) -> None:
    handed: list[object] = []

    def factory(settings, store, writer=None):
        handed.append(writer)
        return []

    app = app_factory(pipeline_factory=factory)
    async with running_app(app):
        assert app.state.database is None
        assert app.state.history is None
    assert handed == [None]  # nothing to offer: the factory is called the old two-arg way


async def test_a_factory_without_a_writer_parameter_still_works_with_persistence_on(
    app_factory, running_app, make_pipeline
) -> None:
    """Factories predating persistence declare `(settings, store)` only; handing them
    a `writer=` keyword would be a TypeError at startup."""
    app = app_factory(database=_FakeDatabase(), pipelines=[make_pipeline(CAMERA)])
    async with running_app(app) as client:
        resp = await client.get("/api/healthz")
    assert resp.status_code == 200
