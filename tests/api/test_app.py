"""App construction: worker isolation, request-id middleware, pipeline lifecycle."""

from __future__ import annotations

import subprocess
import sys


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
