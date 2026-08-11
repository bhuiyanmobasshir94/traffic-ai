"""Camera registry, state, frame, and per-camera event endpoints."""

from __future__ import annotations

import pytest

from traffic_ai.cameras import CAMERAS

CAMERA_ROUTE_TEMPLATES = [
    "/api/cameras/{camera_id}/state",
    "/api/cameras/{camera_id}/frame.jpg",
    "/api/cameras/{camera_id}/stream.mjpg",
    "/api/cameras/{camera_id}/events",
]


async def test_list_cameras(app_factory, running_app) -> None:
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get("/api/cameras")
    assert resp.status_code == 200
    body = resp.json()
    assert {c["camera_id"] for c in body} == {c.camera_id for c in CAMERAS}


@pytest.mark.parametrize("route_template", CAMERA_ROUTE_TEMPLATES)
async def test_unknown_camera_is_404(app_factory, running_app, route_template: str) -> None:
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get(route_template.format(camera_id="does-not-exist"))
    assert resp.status_code == 404


async def test_state_503_before_publish_then_200(
    app_factory, running_app, store, camera, running_state
) -> None:
    app = app_factory()
    async with running_app(app) as client:
        before = await client.get(f"/api/cameras/{camera.camera_id}/state")
        assert before.status_code == 503

        await store.publish_state(running_state)

        after = await client.get(f"/api/cameras/{camera.camera_id}/state")
    assert after.status_code == 200
    assert after.json()["camera_id"] == camera.camera_id


async def test_frame_503_when_absent_then_content_type(
    app_factory, running_app, store, camera
) -> None:
    app = app_factory()
    async with running_app(app) as client:
        before = await client.get(f"/api/cameras/{camera.camera_id}/frame.jpg")
        assert before.status_code == 503

        await store.publish_frame(camera.camera_id, b"\xff\xd8fake-jpeg-bytes")

        after = await client.get(f"/api/cameras/{camera.camera_id}/frame.jpg")
    assert after.status_code == 200
    assert after.headers["content-type"] == "image/jpeg"
    assert after.headers["cache-control"] == "no-store"
    assert after.content == b"\xff\xd8fake-jpeg-bytes"


async def test_camera_events_empty_when_none_published(app_factory, running_app, camera) -> None:
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get(f"/api/cameras/{camera.camera_id}/events")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.parametrize("limit", [0, 201])
async def test_camera_events_limit_rejected_outside_bounds(
    app_factory, running_app, camera, limit: int
) -> None:
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get(f"/api/cameras/{camera.camera_id}/events?limit={limit}")
    assert resp.status_code == 422


@pytest.mark.parametrize("limit", [1, 200])
async def test_camera_events_limit_accepted_at_bounds(
    app_factory, running_app, camera, limit: int
) -> None:
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get(f"/api/cameras/{camera.camera_id}/events?limit={limit}")
    assert resp.status_code == 200
