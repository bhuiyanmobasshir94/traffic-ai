"""Tests for `traffic_ai.ui.client`.

All HTTP is mocked via `httpx.MockTransport` — nothing here talks to a real
worker, so these pass with `src/traffic_ai/api` and `src/traffic_ai/worker`
absent.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from traffic_ai.domain import CameraSummary, CrossingEvent, Direction, PipelineStatus
from traffic_ai.ui.client import WorkerClient, WorkerUnavailable


def _client(handler, *, public_base_url: str = "/api") -> WorkerClient:
    return WorkerClient(
        "http://worker:8000/api",
        transport=httpx.MockTransport(handler),
        public_base_url=public_base_url,
    )


def test_cameras_parses_into_domain_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/cameras"
        return httpx.Response(
            200,
            json=[
                {
                    "camera_id": "toll-plaza-a",
                    "name": "Toll Plaza A",
                    "latitude": 23.8,
                    "longitude": 90.4,
                }
            ],
        )

    cameras = _client(handler).cameras()
    assert cameras == [
        CameraSummary(camera_id="toll-plaza-a", name="Toll Plaza A", latitude=23.8, longitude=90.4)
    ]


def test_connection_error_raises_worker_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(WorkerUnavailable):
        _client(handler).cameras()


def test_timeout_raises_worker_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(WorkerUnavailable):
        _client(handler).events()


def test_state_returns_none_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    assert _client(handler).state("unknown-camera") is None


def test_state_returns_none_on_503() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    assert _client(handler).state("toll-plaza-a") is None


def test_state_raises_on_unexpected_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(WorkerUnavailable):
        _client(handler).state("toll-plaza-a")


def test_state_parses_camera_state() -> None:
    updated_at = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/cameras/toll-plaza-a/state"
        return httpx.Response(
            200,
            json={
                "camera_id": "toll-plaza-a",
                "name": "Toll Plaza A",
                "status": "running",
                "updated_at": updated_at.isoformat(),
            },
        )

    state = _client(handler).state("toll-plaza-a")
    assert state is not None
    assert state.camera_id == "toll-plaza-a"
    assert state.status == PipelineStatus.RUNNING


def test_events_parsed_into_crossing_events() -> None:
    crossed_at = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/events"
        assert request.url.params["limit"] == "10"
        return httpx.Response(
            200,
            json=[
                {
                    "camera_id": "toll-plaza-a",
                    "track_id": 1,
                    "vehicle_class": "car",
                    "direction": "incoming",
                    "crossed_at": crossed_at.isoformat(),
                    "confidence": 0.9,
                }
            ],
        )

    events = _client(handler).events(limit=10)
    assert events == [
        CrossingEvent(
            camera_id="toll-plaza-a",
            track_id=1,
            vehicle_class="car",
            direction=Direction.INCOMING,
            crossed_at=crossed_at,
            confidence=0.9,
        )
    ]


def test_events_for_camera_uses_per_camera_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/cameras/toll-plaza-b/events"
        return httpx.Response(200, json=[])

    assert _client(handler).events("toll-plaza-b") == []


def test_healthy_false_on_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert _client(handler).healthy() is False


def test_healthy_true_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/healthz"
        return httpx.Response(
            200, json={"status": "ok", "version": "1.0.0", "environment": "development"}
        )

    assert _client(handler).healthy() is True


def test_healthy_false_on_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    assert _client(handler).healthy() is False


def test_stream_url_built_from_public_base() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("stream_url must not make a request")

    client = _client(handler, public_base_url="/api")
    assert client.stream_url("toll-plaza-a") == "/api/cameras/toll-plaza-a/stream.mjpg"


def test_states_skips_cameras_with_no_data_yet() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/cameras":
            return httpx.Response(
                200,
                json=[
                    {
                        "camera_id": "toll-plaza-a",
                        "name": "Toll Plaza A",
                        "latitude": 1.0,
                        "longitude": 2.0,
                    },
                    {
                        "camera_id": "toll-plaza-b",
                        "name": "Toll Plaza B",
                        "latitude": 3.0,
                        "longitude": 4.0,
                    },
                ],
            )
        if request.url.path == "/api/cameras/toll-plaza-a/state":
            return httpx.Response(
                200,
                json={
                    "camera_id": "toll-plaza-a",
                    "name": "Toll Plaza A",
                    "status": "running",
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
        if request.url.path == "/api/cameras/toll-plaza-b/state":
            return httpx.Response(503)
        raise AssertionError(f"unexpected path {request.url.path}")

    states = _client(handler).states()
    assert set(states) == {"toll-plaza-a"}
