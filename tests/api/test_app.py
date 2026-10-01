"""App construction: worker isolation, request-id middleware, pipeline lifecycle."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid

import pytest


def test_importing_api_app_does_not_import_worker() -> None:
    """`traffic_ai.api.app` must be importable with `traffic_ai.worker` absent
    from `sys.modules`.

    Checked in a fresh interpreter rather than in-process: this repo's test
    session also collects `tests/worker/`, which imports the worker package
    for its own tests, so an in-process check of this invariant would be a
    false failure once that suite exists alongside this one — as it now does.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import traffic_ai.api.app, sys; "
            "assert not [m for m in sys.modules if m.startswith('traffic_ai.worker')]",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def test_request_id_echoed_and_generated(app_factory, running_app) -> None:
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get("/api/healthz")
        assert "x-request-id" in resp.headers

        resp_with_inbound = await client.get(
            "/api/healthz", headers={"X-Request-ID": "test-request-id"}
        )
    assert resp_with_inbound.headers["x-request-id"] == "test-request-id"


@pytest.mark.parametrize(
    "inbound",
    [
        "0123456789abcdef",
        "550e8400-e29b-41d4-a716-446655440000",
        "trace.id_with-all.the_allowed-chars.0",
        "a",
        "a" * 128,  # the longest accepted
    ],
)
async def test_a_well_formed_inbound_request_id_is_kept(
    app_factory, running_app, inbound: str
) -> None:
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get("/api/healthz", headers={"X-Request-ID": inbound})
    assert resp.headers["x-request-id"] == inbound


@pytest.mark.parametrize(
    "inbound",
    [
        "a" * 129,  # one over the limit
        "a" * 5000,
        "has a space",
        "semi;colon",
        'quote"d',
        "<script>alert(1)</script>",
        "path/../traversal",
        'forged log line {"level": "error"}',
        "caf" + chr(0xE9),  # non-ASCII
        "id\ttab",
        "trailing-newline\n",  # `$` would let this through; the pattern must not
        "-id,with,commas",
    ],
)
async def test_a_malformed_inbound_request_id_is_replaced_with_a_generated_one(
    app_factory, running_app, inbound: str
) -> None:
    """The inbound value is written to every log line of the request and echoed into a response
    header, so it is accepted only if it looks like a trace id. Anything else is not echoed back
    -- a fresh UUID is generated -- and the request is served normally."""
    app = app_factory()
    async with running_app(app) as client:
        # As latin-1 bytes, which is how Starlette reads header values: httpx refuses to
        # encode a non-ASCII `str` header at all.
        resp = await client.get("/api/healthz", headers={"X-Request-ID": inbound.encode("latin-1")})
    assert resp.status_code == 200
    echoed = resp.headers["x-request-id"]
    assert echoed != inbound
    assert uuid.UUID(echoed).version == 4


async def test_a_blank_inbound_request_id_is_replaced(app_factory, running_app) -> None:
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get("/api/healthz", headers={"X-Request-ID": ""})
    assert uuid.UUID(resp.headers["x-request-id"]).version == 4


async def test_the_shutdown_event_is_unset_while_serving_and_set_once_the_app_stops(
    app_factory, running_app
) -> None:
    """Open streams watch this event (see `test_stream.py`); it must flip at the start of
    teardown, and not before."""
    app = app_factory()
    async with running_app(app) as client:
        assert (await client.get("/api/healthz")).status_code == 200
        assert app.state.shutdown_event.is_set() is False
    assert app.state.shutdown_event.is_set() is True


async def test_the_shutdown_event_is_set_before_the_pipelines_are_stopped(
    app_factory, running_app
) -> None:
    """Streams must be told to end first: a pipeline that takes its time to stop must not
    keep every open stream (and so the whole graceful shutdown) waiting behind it."""
    seen: list[bool] = []

    class _Pipeline:
        camera_id = "toll-plaza-a"
        state = None

        def __init__(self) -> None:
            self.app: object = None
            self._stopped = asyncio.Event()

        async def run(self) -> None:
            await self._stopped.wait()

        def request_stop(self) -> None:
            seen.append(self.app.state.shutdown_event.is_set())  # type: ignore[attr-defined]
            self._stopped.set()

    pipeline = _Pipeline()
    app = app_factory(pipelines=[pipeline])  # type: ignore[list-item]
    pipeline.app = app
    async with running_app(app):
        pass

    assert seen == [True]


async def test_crashing_pipeline_is_logged_not_fatal(
    app_factory, running_app, crashing_pipeline
) -> None:
    """A pipeline whose `run()` raises must not take the app down — the
    lifespan wraps every pipeline task and logs the crash instead."""
    app = app_factory(pipelines=[crashing_pipeline])
    async with running_app(app) as client:
        resp = await client.get("/api/healthz")
    assert resp.status_code == 200


async def test_shutdown_stops_pipelines(app_factory, running_app, make_pipeline) -> None:
    pipeline = make_pipeline("toll-plaza-a")
    app = app_factory(pipelines=[pipeline])
    async with running_app(app) as client:
        resp = await client.get("/api/healthz")
        assert resp.status_code == 200
    # Lifespan shutdown must have called request_stop(); the stub's run()
    # only returns once that happens.
    assert pipeline._stop_event.is_set()
