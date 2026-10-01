"""Prometheus metrics: the scrape endpoint, the `path` label, and the registry.

The metric objects live in one process-wide registry (`traffic_ai.metrics.registry`),
so counts accumulate across tests in this session. Every assertion about a
count is therefore a DELTA against a reading taken just before the request,
never an absolute value.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY

from traffic_ai import metrics

TOKEN = "correct-horse-battery-staple-0123456789"  # noqa: S105 - a test fixture, not a credential


def _requests(method: str, path: str, status: str) -> float:
    value = metrics.registry.get_sample_value(
        "http_requests_total", {"method": method, "path": path, "status": status}
    )
    return value or 0.0


def _observations(method: str, path: str) -> float:
    value = metrics.registry.get_sample_value(
        "http_request_duration_seconds_count", {"method": method, "path": path}
    )
    return value or 0.0


def _request_series() -> set[tuple[str, str, str]]:
    """Every (method, path, status) series `http_requests_total` currently holds."""
    series = set()
    for family in metrics.registry.collect():
        if family.name != "http_requests":
            continue
        for sample in family.samples:
            if sample.name == "http_requests_total":
                labels = sample.labels
                series.add((labels["method"], labels["path"], labels["status"]))
    return series


# --- the scrape endpoint -----------------------------------------------------


async def test_metrics_endpoint_serves_prometheus_text(app_factory, running_app) -> None:
    app = app_factory()
    async with running_app(app) as client:
        await client.get("/api/healthz")  # make sure there is something to report
        resp = await client.get("/api/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == CONTENT_TYPE_LATEST
    assert resp.headers["content-type"].startswith("text/plain")
    assert "# TYPE http_requests_total counter" in resp.text
    assert "http_request_duration_seconds_bucket" in resp.text


async def test_metrics_path_is_taken_from_settings(
    app_factory, running_app, settings_factory
) -> None:
    app = app_factory(settings_override=settings_factory(metrics_path="/api/internal/metrics"))
    async with running_app(app) as client:
        moved = await client.get("/api/internal/metrics")
        default = await client.get("/api/metrics")
    assert moved.status_code == 200
    assert default.status_code == 404


async def test_metrics_disabled_means_no_endpoint_and_no_instrumentation(
    app_factory, running_app, settings_factory
) -> None:
    app = app_factory(settings_override=settings_factory(metrics_enabled=False))
    names = [m.kwargs["dispatch"].__name__ for m in app.user_middleware if "dispatch" in m.kwargs]
    assert "_metrics_middleware" not in names

    before = _requests("GET", "/api/healthz", "200")
    async with running_app(app) as client:
        endpoint = await client.get("/api/metrics")
        await client.get("/api/healthz")
    assert endpoint.status_code == 404
    assert _requests("GET", "/api/healthz", "200") == before  # nothing was recorded


async def test_metrics_endpoint_is_covered_by_the_token(
    app_factory, running_app, settings_factory
) -> None:
    """The endpoint sits under /api and is not auth-exempt, so a configured
    token protects it — metrics expose camera ids and traffic volume."""
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    async with running_app(app) as client:
        anonymous = await client.get("/api/metrics")
        authenticated = await client.get(
            "/api/metrics", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert anonymous.status_code == 401
    assert "http_requests_total" not in anonymous.text
    assert authenticated.status_code == 200
    assert "http_requests_total" in authenticated.text


# --- the `path` label --------------------------------------------------------


async def test_path_label_is_the_route_template(app_factory, running_app, camera) -> None:
    template = "/api/cameras/{camera_id}/events"
    before_count = _requests("GET", template, "200")
    before_observed = _observations("GET", template)

    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get(f"/api/cameras/{camera.camera_id}/events")
    assert resp.status_code == 200

    assert _requests("GET", template, "200") == before_count + 1
    assert _observations("GET", template) == before_observed + 1
    # ...and the raw URL is not a label anywhere.
    assert all(camera.camera_id not in path for _, path, _ in _request_series())


async def test_many_distinct_ids_collapse_to_one_series(app_factory, running_app) -> None:
    """The cardinality guarantee: probing N different camera ids must not create
    N Prometheus series. These all 404 inside the route, so they share the
    route's template."""
    template = "/api/cameras/{camera_id}/events"
    before = _request_series()

    app = app_factory()
    async with running_app(app) as client:
        for n in range(25):
            resp = await client.get(f"/api/cameras/probe-{n}/events")
            assert resp.status_code == 404

    added = _request_series() - before
    assert added <= {("GET", template, "404")}
    assert all("probe-" not in path for _, path, _ in _request_series())


async def test_unmatched_paths_share_a_single_label(app_factory, running_app) -> None:
    """A 404 on an attacker-chosen path must not mint a label per probe."""
    before = _requests("GET", "unmatched", "404")
    app = app_factory()
    async with running_app(app) as client:
        for n in range(10):
            resp = await client.get(f"/api/definitely-not-a-route-{n}")
            assert resp.status_code == 404
        scrape = await client.get("/api/metrics")

    assert _requests("GET", "unmatched", "404") == before + 10
    assert "definitely-not-a-route" not in scrape.text
    assert all("definitely-not-a-route" not in path for _, path, _ in _request_series())


async def test_query_strings_are_not_part_of_the_label(app_factory, running_app) -> None:
    template = "/api/events"
    before = _requests("GET", template, "200")
    app = app_factory()
    async with running_app(app) as client:
        await client.get("/api/events?limit=7")
        await client.get("/api/events?limit=8")
    assert _requests("GET", template, "200") == before + 2
    assert all("limit=" not in path for _, path, _ in _request_series())


async def test_rejected_requests_are_still_counted(
    app_factory, running_app, settings_factory
) -> None:
    """Metrics sit outside auth, so a 401 is counted. It never reached the
    router, so it has no route template and is labelled `unmatched`."""
    before = _requests("GET", "unmatched", "401")
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    async with running_app(app) as client:
        resp = await client.get("/api/cameras")
    assert resp.status_code == 401
    assert _requests("GET", "unmatched", "401") == before + 1


async def test_rate_limited_requests_are_still_counted(
    app_factory, running_app, settings_factory
) -> None:
    before = _requests("GET", "unmatched", "429")
    app = app_factory(settings_override=settings_factory(rate_limit_requests=1))
    async with running_app(app) as client:
        await client.get("/api/cameras")
        limited = await client.get("/api/cameras")
    assert limited.status_code == 429
    assert _requests("GET", "unmatched", "429") == before + 1


async def test_an_unhandled_exception_is_counted_as_a_500(app_factory, running_app) -> None:
    async def boom() -> None:
        raise RuntimeError("boom")

    app = app_factory()
    app.add_api_route("/api/boom", boom, methods=["GET"])
    before = _requests("GET", "/api/boom", "500")
    async with running_app(app) as client:
        with pytest.raises(RuntimeError, match="boom"):
            await client.get("/api/boom")
    assert _requests("GET", "/api/boom", "500") == before + 1


# --- the registry and the pipeline metrics ----------------------------------


def test_render_returns_payload_and_content_type() -> None:
    payload, content_type = metrics.render()
    assert isinstance(payload, bytes)
    assert content_type == CONTENT_TYPE_LATEST
    assert b"http_requests_total" in payload


def test_uses_a_private_registry_not_the_global_default() -> None:
    assert metrics.registry is not REGISTRY
    global_names = {s.name for family in REGISTRY.collect() for s in family.samples}
    ours = ("http_request", "pipeline_", "crossings")
    assert not [name for name in global_names if name.startswith(ours)]


def test_module_can_be_reimported_without_duplicate_registration() -> None:
    """A module-level metric on the DEFAULT registry raises `Duplicated
    timeseries` the second time it is imported. Run in a fresh interpreter so
    the reload cannot disturb this session's metric objects."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib, traffic_ai.metrics as m; importlib.reload(m); "
            "from prometheus_client import REGISTRY; "
            "names = {s.name for f in REGISTRY.collect() for s in f.samples}; "
            "assert not any(n.startswith('http_request') for n in names)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_pipeline_metrics_are_settable_and_rendered() -> None:
    camera = "metrics-test-camera"
    metrics.pipeline_frames_processed_total.labels(camera).inc(3)
    metrics.pipeline_fps.labels(camera).set(11.5)
    metrics.pipeline_active_tracks.labels(camera).set(4)
    metrics.pipeline_status.labels(camera).state("running")
    metrics.crossings_total.labels(camera, "inbound", "car").inc(2)

    payload, _ = metrics.render()
    text = payload.decode()
    assert f'pipeline_frames_processed_total{{camera_id="{camera}"}} 3.0' in text
    assert f'pipeline_fps{{camera_id="{camera}"}} 11.5' in text
    assert f'pipeline_active_tracks{{camera_id="{camera}"}} 4.0' in text
    assert f'pipeline_status{{camera_id="{camera}",pipeline_status="running"}} 1.0' in text
    assert f'pipeline_status{{camera_id="{camera}",pipeline_status="stalled"}} 0.0' in text
    assert (
        f'crossings_total{{camera_id="{camera}",direction="inbound",vehicle_class="car"}} 2.0'
        in text
    )


def test_pipeline_status_states_match_the_domain_enum() -> None:
    """The Enum's states are derived from `PipelineStatus`, so a status the
    worker can report is always one the metric accepts."""
    from traffic_ai.domain import PipelineStatus

    camera = "metrics-test-status-states"
    for status in PipelineStatus:
        metrics.pipeline_status.labels(camera).state(status.value)  # must not raise
    with pytest.raises(ValueError, match="not-a-status"):
        metrics.pipeline_status.labels(camera).state("not-a-status")
