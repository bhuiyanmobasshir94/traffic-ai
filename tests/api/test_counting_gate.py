"""A camera whose counting is switched off (`CameraConfig.counting_enabled=False`) serves
no crossings on any API path, and says why; its live video and state are unaffected.

- 409, with the camera's own `counting_disabled_reason` as `detail`, on
  `/api/cameras/{id}/events` and on `/api/history/{events,counts,hourly}` when
  `camera_id` names it.
- `/api/events` (every camera) leaves it out, even if Redis still holds its events.
- `/api/cameras/{id}/state`, `frame.jpg` and `stream.mjpg` stay 200: the pipeline is live
  and `active_tracks` is real.

Order of answers, pinned below: unknown camera (404) -> not calibrated (409) -> a bad
window or `limit` (422) -> history unavailable (503). The 409 is decided in the first
dependency of each route, so a caller is never told to fix a window for a camera that
could not answer it however the window was written.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from traffic_ai import cameras as cameras_module
from traffic_ai.api.dependencies import get_history_reader
from traffic_ai.cameras import CAMERAS, DEFAULT_COUNTING_DISABLED_REASON
from traffic_ai.domain import CameraState, CrossingEvent, Direction, PipelineStatus

UNCALIBRATED = next(c for c in CAMERAS if not c.counting_enabled)
CALIBRATED = next(c for c in CAMERAS if c.counting_enabled)
REASON = UNCALIBRATED.counting_disabled_reason

HISTORY_PATHS = ["/api/history/events", "/api/history/counts", "/api/history/hourly"]


class _RecordingHistory:
    """A history source that answers every query with an empty-but-valid result, and
    records the call. If the gate were missing, a camera B request would be a 200 with
    zeros: exactly what the 409 exists to prevent."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def recent(self, camera_id: str | None, limit: int) -> list[CrossingEvent]:
        self.calls.append(("recent", camera_id, limit))
        return []

    async def counts_by_class(
        self, camera_id: str | None, since: datetime, until: datetime
    ) -> dict[str, dict[str, int]]:
        self.calls.append(("counts", camera_id, since, until))
        return {}

    async def hourly_totals(
        self, camera_id: str | None, since: datetime, until: datetime
    ) -> list[tuple[datetime, int]]:
        self.calls.append(("hourly", camera_id, since, until))
        return []


@pytest.fixture
def history() -> _RecordingHistory:
    return _RecordingHistory()


@pytest.fixture
def history_app(app_factory, history) -> Callable[[], Any]:
    def build():
        app = app_factory()
        app.dependency_overrides[get_history_reader] = lambda: history
        return app

    return build


def _event(camera_id: str, track_id: int, *, seconds_ago: float) -> CrossingEvent:
    return CrossingEvent(
        camera_id=camera_id,
        track_id=track_id,
        vehicle_class="car",
        direction=Direction.INCOMING,
        crossed_at=datetime.now(UTC) - timedelta(seconds=seconds_ago),
        confidence=0.9,
    )


# --- history ---------------------------------------------------------------------


@pytest.mark.parametrize("path", HISTORY_PATHS)
async def test_history_for_an_uncalibrated_camera_is_409_with_the_reason_and_never_queried(
    history_app, running_app, history, path: str
) -> None:
    async with running_app(history_app()) as client:
        resp = await client.get(path, params={"camera_id": UNCALIBRATED.camera_id})

    assert resp.status_code == 409
    assert resp.json() == {"detail": REASON}
    assert history.calls == []


@pytest.mark.parametrize("path", HISTORY_PATHS)
async def test_history_for_a_camera_that_counts_still_reaches_the_database(
    history_app, running_app, history, path: str
) -> None:
    async with running_app(history_app()) as client:
        resp = await client.get(path, params={"camera_id": CALIBRATED.camera_id})

    assert resp.status_code == 200
    assert [call[1] for call in history.calls] == [CALIBRATED.camera_id]


@pytest.mark.parametrize("path", HISTORY_PATHS)
async def test_history_without_a_camera_filter_is_not_gated(
    history_app, running_app, history, path: str
) -> None:
    """Pins the boundary of the gate: it is decided by the `camera_id` the caller names. The
    unfiltered aggregates are whatever the database holds; the API cannot take one camera
    out of a SQL total without a change to the repository."""
    async with running_app(history_app()) as client:
        resp = await client.get(path)

    assert resp.status_code == 200
    assert [call[1] for call in history.calls] == [None]


@pytest.mark.parametrize(
    ("path", "params"),
    [
        # A naive datetime: the window dependency raises 422 itself.
        ("/api/history/counts", {"since": "2026-03-01T10:00:00"}),
        ("/api/history/hourly", {"since": "2026-03-01T10:00:00"}),
        # An empty window: also raised by the window dependency.
        (
            "/api/history/counts",
            {"since": "2026-03-02T00:00:00Z", "until": "2026-03-01T00:00:00Z"},
        ),
        # An unparseable datetime: a validation error collected by FastAPI itself.
        ("/api/history/counts", {"since": "not-a-date"}),
        ("/api/history/hourly", {"until": "not-a-date"}),
        # A `limit` outside its bounds: the endpoint's own parameter.
        ("/api/history/events", {"limit": "0"}),
        ("/api/history/events", {"limit": "1001"}),
    ],
)
async def test_409_is_answered_before_a_window_or_limit_error(
    history_app, running_app, history, path: str, params: dict[str, str]
) -> None:
    async with running_app(history_app()) as client:
        # The same request for a camera that counts really is a 422...
        invalid = await client.get(path, params={**params, "camera_id": CALIBRATED.camera_id})
        # ...so for the uncalibrated one the 409 is what is taking precedence.
        gated = await client.get(path, params={**params, "camera_id": UNCALIBRATED.camera_id})

    assert invalid.status_code == 422
    assert gated.status_code == 409
    assert gated.json() == {"detail": REASON}
    assert history.calls == []


@pytest.mark.parametrize("path", HISTORY_PATHS)
async def test_409_is_answered_before_history_is_found_unavailable(
    app_factory, running_app, path: str
) -> None:
    """With persistence off, a camera that counts is a 503. The uncalibrated camera is a
    409: telling its caller "the database is down" would imply history would exist if it
    were up."""
    async with running_app(app_factory()) as client:
        unavailable = await client.get(path, params={"camera_id": CALIBRATED.camera_id})
        gated = await client.get(path, params={"camera_id": UNCALIBRATED.camera_id})

    assert unavailable.status_code == 503
    assert gated.status_code == 409


@pytest.mark.parametrize("path", HISTORY_PATHS)
async def test_an_unknown_camera_is_still_404_not_409(
    history_app, running_app, history, path: str
) -> None:
    async with running_app(history_app()) as client:
        resp = await client.get(path, params={"camera_id": "no-such-camera"})

    assert resp.status_code == 404
    assert history.calls == []


# --- per-camera live events --------------------------------------------------------


async def test_camera_events_for_an_uncalibrated_camera_is_409_even_if_redis_holds_events(
    app_factory, running_app, store
) -> None:
    """Redis events can outlive the switch (up to the state TTL): they are not served."""
    await store.append_events([_event(UNCALIBRATED.camera_id, 1, seconds_ago=5)])

    async with running_app(app_factory()) as client:
        resp = await client.get(f"/api/cameras/{UNCALIBRATED.camera_id}/events")

    assert resp.status_code == 409
    assert resp.json() == {"detail": REASON}


async def test_camera_events_409_is_answered_before_a_bad_limit(app_factory, running_app) -> None:
    async with running_app(app_factory()) as client:
        invalid = await client.get(f"/api/cameras/{CALIBRATED.camera_id}/events?limit=0")
        gated = await client.get(f"/api/cameras/{UNCALIBRATED.camera_id}/events?limit=0")

    assert invalid.status_code == 422
    assert gated.status_code == 409


async def test_camera_events_for_an_unknown_camera_is_404_not_409(app_factory, running_app) -> None:
    async with running_app(app_factory()) as client:
        resp = await client.get("/api/cameras/no-such-camera/events")

    assert resp.status_code == 404


async def test_camera_events_for_a_camera_that_counts_are_unchanged(
    app_factory, running_app, store
) -> None:
    await store.append_events([_event(CALIBRATED.camera_id, 7, seconds_ago=5)])

    async with running_app(app_factory()) as client:
        resp = await client.get(f"/api/cameras/{CALIBRATED.camera_id}/events")

    assert resp.status_code == 200
    assert [e["track_id"] for e in resp.json()] == [7]


# --- the all-cameras feed ----------------------------------------------------------


async def test_all_events_leaves_out_an_uncalibrated_camera_even_when_redis_holds_its_events(
    app_factory, running_app, store
) -> None:
    await store.append_events(
        [
            _event(CALIBRATED.camera_id, track_id, seconds_ago=60 - track_id)
            for track_id in (1, 2, 3)
        ]
    )
    # Newer than every event of the camera that counts, so a filter applied after the
    # `limit` would have these fill the page and leave nothing.
    await store.append_events(
        [
            _event(UNCALIBRATED.camera_id, track_id, seconds_ago=track_id - 100)
            for track_id in (101, 102)
        ]
    )

    async with running_app(app_factory()) as client:
        everything = await client.get("/api/events")
        limited = await client.get("/api/events?limit=2")

    assert everything.status_code == limited.status_code == 200
    assert {e["camera_id"] for e in everything.json()} == {CALIBRATED.camera_id}
    assert sorted(e["track_id"] for e in everything.json()) == [1, 2, 3]
    # The limit is spent on events that are served, not on ones about to be dropped.
    assert [e["track_id"] for e in limited.json()] == [3, 2]


# --- what stays live ----------------------------------------------------------------


async def test_state_frame_and_stream_of_an_uncalibrated_camera_are_still_served(
    app_factory, running_app, store, settings
) -> None:
    jpeg = b"\xff\xd8fake-jpeg-bytes"
    camera_id = UNCALIBRATED.camera_id
    await store.publish_state(
        CameraState(
            camera_id=camera_id,
            name=UNCALIBRATED.name,
            status=PipelineStatus.RUNNING,
            updated_at=datetime.now(UTC),
            active_tracks=7,
            counting_enabled=False,
        )
    )
    await store.publish_frame(camera_id, jpeg)

    app = app_factory(settings_override=settings.model_copy(update={"state_ttl_seconds": 5}))
    async with running_app(app) as client:
        state = await client.get(f"/api/cameras/{camera_id}/state")
        frame = await client.get(f"/api/cameras/{camera_id}/frame.jpg")
        async with client.stream("GET", f"/api/cameras/{camera_id}/stream.mjpg") as stream:
            stream_status = stream.status_code
            stream_type = stream.headers["content-type"]

    assert state.status_code == 200
    assert state.json()["counting_enabled"] is False
    assert state.json()["active_tracks"] == 7
    assert frame.status_code == 200
    assert frame.content == jpeg
    assert stream_status == 200
    assert stream_type == "multipart/x-mixed-replace; boundary=frame"


# --- the contract ------------------------------------------------------------------


async def test_a_409_with_no_configured_reason_still_says_something(
    app_factory, running_app, monkeypatch
) -> None:
    """A camera switched off with no reason is still refused, with the generic one: failing
    to explain must not turn into serving the data."""
    bare = dataclasses.replace(UNCALIBRATED, counting_disabled_reason="")
    monkeypatch.setitem(cameras_module.CAMERAS_BY_ID, bare.camera_id, bare)

    async with running_app(app_factory()) as client:
        resp = await client.get(f"/api/cameras/{bare.camera_id}/events")

    assert resp.status_code == 409
    assert resp.json() == {"detail": DEFAULT_COUNTING_DISABLED_REASON}


@pytest.mark.parametrize(
    "path",
    [
        "/api/cameras/{camera_id}/events",
        "/api/history/events",
        "/api/history/counts",
        "/api/history/hourly",
    ],
)
async def test_openapi_documents_the_409(app_factory, path: str) -> None:
    schema = app_factory().openapi()

    assert "409" in schema["paths"][path]["get"]["responses"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/cameras/{camera_id}/state",
        "/api/cameras/{camera_id}/frame.jpg",
        "/api/cameras/{camera_id}/stream.mjpg",
        "/api/events",
    ],
)
async def test_openapi_does_not_claim_a_409_where_there_is_none(app_factory, path: str) -> None:
    schema = app_factory().openapi()

    assert "409" not in schema["paths"][path]["get"]["responses"]
