"""The merged, cross-camera crossing-events feed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traffic_ai.cameras import CAMERAS
from traffic_ai.domain import CrossingEvent, Direction


def _event(camera_id: str, track_id: int, *, seconds_ago: float) -> CrossingEvent:
    return CrossingEvent(
        camera_id=camera_id,
        track_id=track_id,
        vehicle_class="car",
        direction=Direction.INCOMING,
        crossed_at=datetime.now(UTC) - timedelta(seconds=seconds_ago),
        confidence=0.9,
    )


async def test_events_merged_newest_first(app_factory, running_app, store) -> None:
    camera_a, camera_b = CAMERAS[0].camera_id, CAMERAS[1].camera_id
    oldest = _event(camera_a, 1, seconds_ago=30)
    middle = _event(camera_b, 2, seconds_ago=20)
    newest = _event(camera_a, 3, seconds_ago=5)

    await store.append_events([oldest])
    await store.append_events([middle])
    await store.append_events([newest])

    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get("/api/events")
    assert resp.status_code == 200
    body = resp.json()
    track_ids = [e["track_id"] for e in body]
    assert track_ids == [3, 2, 1]
    assert {e["camera_id"] for e in body} == {camera_a, camera_b}


async def test_events_empty_when_none_published(app_factory, running_app) -> None:
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get("/api/events")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.parametrize("limit", [0, 201])
async def test_events_limit_rejected_outside_bounds(app_factory, running_app, limit: int) -> None:
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get(f"/api/events?limit={limit}")
    assert resp.status_code == 422


async def test_events_respects_limit(app_factory, running_app, store) -> None:
    camera_a = CAMERAS[0].camera_id
    for i in range(5):
        await store.append_events([_event(camera_a, i, seconds_ago=i)])

    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get("/api/events?limit=2")
    assert resp.status_code == 200
    assert len(resp.json()) == 2
