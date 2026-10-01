"""Tests for `traffic_ai.ui.client`.

All HTTP is mocked via `httpx.MockTransport` — nothing here talks to a real
worker, so these pass with `src/traffic_ai/api` and `src/traffic_ai/worker`
absent.
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from traffic_ai.cameras import DEFAULT_COUNTING_DISABLED_REASON
from traffic_ai.config import Settings
from traffic_ai.domain import CameraSummary, CrossingEvent, Direction, PipelineStatus
from traffic_ai.ui.client import (
    ApiUnauthorized,
    CountingNotCalibrated,
    HistoryCounts,
    HistoryHourly,
    HistoryUnavailable,
    WorkerClient,
    WorkerUnavailable,
    build_worker_client,
)


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


# --- bearer token -------------------------------------------------------------

TOKEN = "tok-3f9a1c7e5b2d4a60-not-a-real-credential"  # noqa: S105 - a test fixture, not a credential
_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_SINCE = _NOW - timedelta(hours=24)

_COUNTS_BODY = {
    "camera_id": "toll-plaza-a",
    "since": "2026-09-30T12:00:00+00:00",
    "until": "2026-10-01T12:00:00+00:00",
    "counts": {
        "incoming": {"car": 12, "motorcycle": 3, "bus": 1, "truck": 0, "bicycle": 0},
        "outgoing": {"car": 9, "truck": 2},
    },
    "total": 27,
}

_HOURLY_BODY = {
    "camera_id": None,
    "since": "2026-09-30T12:00:00+00:00",
    "until": "2026-10-01T12:00:00+00:00",
    "buckets": [
        {"hour": "2026-10-01T10:00:00+00:00", "total": 14},
        {"hour": "2026-10-01T11:00:00+00:00", "total": 13},
    ],
}


def _authed_client(handler, *, token: str | None = TOKEN) -> WorkerClient:
    return WorkerClient(
        "http://worker:8000/api",
        transport=httpx.MockTransport(handler),
        api_token=token,
    )


def _exception_text(exc: BaseException) -> str:
    """Everything a log line or an error page could show for `exc`: its message,
    its repr, and the full formatted chain (message plus causes)."""
    return " ".join([str(exc), repr(exc), "".join(traceback.format_exception(exc))])


def test_token_is_sent_as_bearer_on_every_kind_of_request() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/state"):
            return httpx.Response(503)
        if request.url.path == "/api/history/counts":
            return httpx.Response(200, json=_COUNTS_BODY)
        if request.url.path == "/api/history/hourly":
            return httpx.Response(200, json=_HOURLY_BODY)
        return httpx.Response(200, json=[])

    client = _authed_client(handler)
    client.cameras()
    client.state("toll-plaza-a")
    client.events()
    client.events("toll-plaza-a")
    client.healthy()
    client.history_events()
    client.history_counts(since=_SINCE, until=_NOW)
    client.history_hourly(since=_SINCE, until=_NOW)

    assert len(seen) == 8
    assert {header for _, header in seen} == {f"Bearer {TOKEN}"}


def test_no_authorization_header_when_no_token_is_configured() -> None:
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, json=[])

    client = _authed_client(handler, token=None)
    client.cameras()
    client.healthy()
    client.history_events()

    assert len(seen) == 3
    assert all("authorization" not in headers for headers in seen)


def test_blank_token_is_treated_as_no_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json=[])

    _authed_client(handler, token="").cameras()


def test_token_with_control_characters_is_refused_without_echoing_it() -> None:
    # Left alone, the HTTP layer rejects this at send time with an error that quotes
    # the whole header value, token included.
    secret = "tok-secret-value"  # noqa: S105 - a test fixture, not a credential
    with pytest.raises(ValueError, match="TRAFFIC_AI_API_TOKEN") as excinfo:
        _authed_client(lambda request: httpx.Response(200), token=f"{secret}\n")
    assert secret not in _exception_text(excinfo.value)

    with pytest.raises(ValueError, match="TRAFFIC_AI_API_TOKEN"):
        _authed_client(lambda request: httpx.Response(200), token="two words")  # noqa: S106


def test_token_never_appears_in_errors_or_repr() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/history/hourly":
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/api/history/events":
            # A buggy server echoing the credential back must not reach us either.
            return httpx.Response(200, text=f"not json, token was {TOKEN}")
        statuses = {"/api/cameras": 500, "/api/events": 401, "/api/history/counts": 503}
        # The body echoes the credential too; the client must not surface bodies.
        return httpx.Response(statuses[request.url.path], json={"detail": f"bad {TOKEN}"})

    client = _authed_client(handler)
    assert TOKEN not in repr(client)

    calls = (
        client.cameras,
        client.events,
        lambda: client.history_counts(since=_SINCE, until=_NOW),
        lambda: client.history_hourly(since=_SINCE, until=_NOW),
        client.history_events,
    )
    for call in calls:
        with pytest.raises(WorkerUnavailable) as excinfo:
            call()
        assert TOKEN not in _exception_text(excinfo.value)


# --- 401 / 503 mapping --------------------------------------------------------


def test_401_raises_api_unauthorized_naming_the_setting() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid or missing API token"})

    client = _authed_client(handler, token=None)
    calls = (
        client.cameras,
        lambda: client.state("toll-plaza-a"),
        client.events,
        client.history_events,
        lambda: client.history_counts(since=_SINCE, until=_NOW),
        lambda: client.history_hourly(since=_SINCE, until=_NOW),
    )
    for call in calls:
        with pytest.raises(ApiUnauthorized, match="check TRAFFIC_AI_API_TOKEN"):
            call()


def test_unauthorized_and_history_unavailable_stay_catchable_as_worker_unavailable() -> None:
    # Existing callers (the dashboard) only know `WorkerUnavailable`; a new
    # subclass must degrade them, not crash them.
    assert issubclass(ApiUnauthorized, WorkerUnavailable)
    assert issubclass(HistoryUnavailable, WorkerUnavailable)
    assert not issubclass(ApiUnauthorized, HistoryUnavailable)
    assert not issubclass(HistoryUnavailable, ApiUnauthorized)


def test_history_503_raises_history_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "history is not enabled"})

    client = _client(handler)
    calls = (
        client.history_events,
        lambda: client.history_counts(since=_SINCE, until=_NOW),
        lambda: client.history_hourly(since=_SINCE, until=_NOW),
    )
    for call in calls:
        with pytest.raises(HistoryUnavailable, match="History unavailable"):
            call()


@pytest.mark.parametrize("status", [404, 422, 500])
def test_other_history_errors_are_generic_worker_unavailable(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "nope"})

    with pytest.raises(WorkerUnavailable, match=str(status)) as excinfo:
        _client(handler).history_counts(since=_SINCE, until=_NOW)
    assert not isinstance(excinfo.value, ApiUnauthorized | HistoryUnavailable)


def test_history_connection_error_is_generic_worker_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(WorkerUnavailable) as excinfo:
        _client(handler).history_hourly(since=_SINCE, until=_NOW)
    assert not isinstance(excinfo.value, ApiUnauthorized | HistoryUnavailable)


# --- 409: counting is not calibrated for the camera ---------------------------


def test_409_raises_counting_not_calibrated_on_every_crossings_route() -> None:
    """The API's 409 means "this camera has no crossings to serve". It must not read as
    "worker returned 409" (a generic fault), as an outage, or as history being down."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "worker-supplied text that is not echoed"})

    client = _client(handler)
    calls = (
        lambda: client.events("toll-plaza-b"),
        client.history_events,
        lambda: client.history_counts(since=_SINCE, until=_NOW, camera_id="toll-plaza-b"),
        lambda: client.history_hourly(since=_SINCE, until=_NOW, camera_id="toll-plaza-b"),
    )
    for call in calls:
        with pytest.raises(CountingNotCalibrated) as excinfo:
            call()
        # The fixed registry wording, not whatever the response body carried.
        assert str(excinfo.value) == DEFAULT_COUNTING_DISABLED_REASON
        assert "worker-supplied" not in str(excinfo.value)
        assert not isinstance(excinfo.value, ApiUnauthorized | HistoryUnavailable)


def test_counting_not_calibrated_stays_catchable_as_worker_unavailable() -> None:
    # A caller that only knows `WorkerUnavailable` degrades rather than crashing.
    assert issubclass(CountingNotCalibrated, WorkerUnavailable)
    assert not issubclass(CountingNotCalibrated, ApiUnauthorized | HistoryUnavailable)
    assert not issubclass(ApiUnauthorized, CountingNotCalibrated)
    assert not issubclass(HistoryUnavailable, CountingNotCalibrated)


def test_409_does_not_disturb_the_neighbouring_status_mappings() -> None:
    # 401 and 503 keep their own errors; 500 stays generic.
    client = _client(
        lambda request: httpx.Response(
            {"/api/history/counts": 401, "/api/history/hourly": 503}.get(request.url.path, 500),
            json={"detail": "x"},
        )
    )
    with pytest.raises(ApiUnauthorized):
        client.history_counts(since=_SINCE, until=_NOW)
    with pytest.raises(HistoryUnavailable):
        client.history_hourly(since=_SINCE, until=_NOW)
    with pytest.raises(WorkerUnavailable, match="500") as excinfo:
        client.history_events()
    assert not isinstance(excinfo.value, CountingNotCalibrated)


# --- history parsing ----------------------------------------------------------


def test_history_counts_parses_the_documented_shape_and_sends_the_window() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/history/counts"
        params = request.url.params
        assert params["camera_id"] == "toll-plaza-a"
        assert datetime.fromisoformat(params["since"]) == _SINCE
        assert datetime.fromisoformat(params["until"]) == _NOW
        return httpx.Response(200, json=_COUNTS_BODY)

    result = _client(handler).history_counts(since=_SINCE, until=_NOW, camera_id="toll-plaza-a")

    assert isinstance(result, HistoryCounts)
    assert result.camera_id == "toll-plaza-a"
    assert result.since == _SINCE
    assert result.until == _NOW
    assert result.total == 27
    assert result.counts[Direction.INCOMING]["car"] == 12
    assert result.counts[Direction.OUTGOING] == {"car": 9, "truck": 2}


def test_history_counts_all_cameras_omits_camera_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "camera_id" not in request.url.params
        return httpx.Response(200, json={**_COUNTS_BODY, "camera_id": None})

    assert _client(handler).history_counts(since=_SINCE, until=_NOW).camera_id is None


def test_history_window_is_sent_in_utc_whatever_the_callers_zone() -> None:
    dhaka = timezone(timedelta(hours=6))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["since"] == _SINCE.isoformat()
        assert request.url.params["until"] == _NOW.isoformat()
        return httpx.Response(200, json=_HOURLY_BODY)

    _client(handler).history_hourly(since=_SINCE.astimezone(dhaka), until=_NOW.astimezone(dhaka))


def test_history_window_rejects_naive_datetimes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a naive window must not be sent")

    with pytest.raises(ValueError, match="timezone-aware"):
        _client(handler).history_counts(since=datetime(2026, 10, 1), until=_NOW)


def test_history_hourly_parses_the_documented_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/history/hourly"
        assert "camera_id" not in request.url.params
        return httpx.Response(200, json=_HOURLY_BODY)

    result = _client(handler).history_hourly(since=_SINCE, until=_NOW)

    assert isinstance(result, HistoryHourly)
    assert result.camera_id is None
    assert [(b.hour.hour, b.total) for b in result.buckets] == [(10, 14), (11, 13)]


def test_history_hourly_with_no_buckets_is_an_empty_result_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**_HOURLY_BODY, "buckets": []})

    assert _client(handler).history_hourly(since=_SINCE, until=_NOW).buckets == []


def test_history_events_parses_crossing_events_and_keeps_plate_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/history/events"
        assert request.url.params["limit"] == "50"
        assert request.url.params["camera_id"] == "toll-plaza-b"
        return httpx.Response(
            200,
            json=[
                {
                    "camera_id": "toll-plaza-b",
                    "track_id": 7,
                    "vehicle_class": "bus",
                    "direction": "outgoing",
                    "crossed_at": "2026-10-01T11:58:00+00:00",
                    "confidence": 0.82,
                    "plate_text": None,
                    "plate_confidence": None,
                }
            ],
        )

    events = _client(handler).history_events("toll-plaza-b", limit=50)

    assert len(events) == 1
    assert events[0].vehicle_class == "bus"
    assert events[0].direction == Direction.OUTGOING
    assert events[0].plate_text is None


def test_history_events_defaults_to_all_cameras() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "camera_id" not in request.url.params
        return httpx.Response(200, json=[])

    assert _client(handler).history_events() == []


@pytest.mark.parametrize(
    "body",
    [
        {"unexpected": "shape"},
        {**_COUNTS_BODY, "total": -1},
        {**_COUNTS_BODY, "counts": {"sideways": {"car": 1}}},
        "not-an-object",
    ],
)
def test_history_counts_rejects_a_malformed_body_visibly(body: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(WorkerUnavailable, match="unexpected body"):
        _client(handler).history_counts(since=_SINCE, until=_NOW)


def test_history_events_rejects_non_json_visibly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy error</html>")

    with pytest.raises(WorkerUnavailable, match="unexpected body"):
        _client(handler).history_events()


# --- settings wiring ----------------------------------------------------------


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_build_worker_client_sends_the_configured_secret() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "worker"
        assert request.url.path == "/api/cameras"
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=[])

    settings = _settings(api_token=TOKEN, api_internal_url="http://worker:8000")
    build_worker_client(settings, transport=httpx.MockTransport(handler)).cameras()

    assert seen == [f"Bearer {TOKEN}"]


def test_build_worker_client_without_token_sends_no_authorization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json=[])

    settings = _settings(api_token=None)
    build_worker_client(settings, transport=httpx.MockTransport(handler)).cameras()


def test_build_worker_client_treats_a_blank_token_as_none() -> None:
    # compose passes `${TRAFFIC_AI_API_TOKEN:-}`, i.e. an empty string, when unset.
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json=[])

    settings = _settings(api_token="")
    build_worker_client(settings, transport=httpx.MockTransport(handler)).cameras()


# --- history_events_lost (advisory: unknown is None, never an error) -----------


@pytest.mark.parametrize("status", [200, 503])
def test_history_events_lost_reads_the_count_whatever_the_readiness_status(status: int) -> None:
    """`/readyz` is 503 when the worker is not ready but the body is still the answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/readyz"
        return httpx.Response(status, json={"ready": status == 200, "history_events_lost": 7})

    assert _client(handler).history_events_lost() == 7


def test_history_events_lost_zero_is_a_real_zero() -> None:
    client = _client(lambda request: httpx.Response(200, json={"history_events_lost": 0}))
    assert client.history_events_lost() == 0


@pytest.mark.parametrize(
    "body",
    [
        {"history_events_lost": None},  # persistence off: not "zero lost"
        {"ready": True},  # an older worker that does not report it
        {"history_events_lost": -1},
        {"history_events_lost": True},  # a bool is not a count
        {"history_events_lost": "7"},
        [],
        "nope",
    ],
)
def test_history_events_lost_is_none_when_the_worker_did_not_say(body: object) -> None:
    client = _client(lambda request: httpx.Response(200, json=body))
    assert client.history_events_lost() is None


def test_history_events_lost_is_none_when_the_worker_cannot_be_asked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert _client(handler).history_events_lost() is None


def test_history_events_lost_is_none_for_a_non_json_body() -> None:
    client = _client(lambda request: httpx.Response(502, text="<html>bad gateway</html>"))
    assert client.history_events_lost() is None
