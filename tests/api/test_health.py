"""Liveness and readiness probes."""

from __future__ import annotations

import pytest

from traffic_ai.domain import PipelineStatus


async def test_healthz_always_200(app_factory, running_app) -> None:
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get("/api/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["environment"]


async def test_healthz_does_not_depend_on_redis(app_factory, running_app, broken_store) -> None:
    """Liveness must not depend on Redis — otherwise a Redis blip triggers a
    container restart loop."""
    app = app_factory(store_override=broken_store)
    async with running_app(app) as client:
        resp = await client.get("/api/healthz")
    assert resp.status_code == 200


async def test_readyz_503_when_redis_down(
    app_factory, running_app, broken_store, make_pipeline
) -> None:
    app = app_factory(
        pipelines=[make_pipeline("toll-plaza-a")],
        store_override=broken_store,
    )
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["redis"] is False


async def test_readyz_503_when_no_pipelines_running(app_factory, running_app) -> None:
    app = app_factory(pipelines=[])
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["cameras_running"] == 0
    assert body["cameras_total"] == 0


async def test_readyz_503_when_all_pipelines_errored(
    app_factory, running_app, make_pipeline
) -> None:
    app = app_factory(pipelines=[make_pipeline("toll-plaza-a", PipelineStatus.ERROR)])
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.status_code == 503
    assert resp.json()["cameras_running"] == 0


async def test_readyz_200_when_ready(app_factory, running_app, make_pipeline) -> None:
    app = app_factory(pipelines=[make_pipeline("toll-plaza-a", PipelineStatus.RUNNING)])
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["cameras_running"] == 1
    assert body["cameras_total"] == 1


@pytest.mark.parametrize(
    "status",
    [PipelineStatus.STARTING, PipelineStatus.STALLED, PipelineStatus.STOPPED],
)
async def test_readyz_503_unless_a_pipeline_is_actually_running(
    app_factory, running_app, make_pipeline, status
) -> None:
    """Only RUNNING counts toward readiness.

    STOPPED is the case that matters operationally: it is what a container
    reports while draining on shutdown. Counting it as ready would keep a load
    balancer sending traffic to an instance that has stopped serving. STARTING
    has nothing to serve yet and STALLED cannot produce a fresh frame, so
    neither is ready either.
    """
    app = app_factory(pipelines=[make_pipeline("toll-plaza-a", status)])
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["cameras_running"] == 0
    assert body["cameras_total"] == 1
