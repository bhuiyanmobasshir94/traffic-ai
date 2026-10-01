"""Tests for the Analytics page: its pure helpers, the status classification in
`fetch_history`, and an `AppTest` render of `pages/Analytics.py`.

All HTTP is `httpx.MockTransport`; nothing needs a worker or a database. The
render tests install a real `WorkerClient` over that transport in place of
`get_worker_client`, so the page exercises the genuine client code (header,
status mapping, parsing) rather than a hand-rolled fake.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from traffic_ai.cameras import CAMERAS
from traffic_ai.domain import CrossingEvent, Direction
from traffic_ai.ui import analytics
from traffic_ai.ui.analytics import (
    ALL_CAMERAS,
    DEFAULT_WINDOW,
    WINDOWS,
    HistoryStatus,
    camera_label,
    camera_options,
    counts_frame,
    direction_totals,
    fetch_history,
    hourly_frame,
    resolve_camera,
    window_bounds,
)
from traffic_ai.ui.client import HistoryCounts, HistoryHourly, WorkerClient
from traffic_ai.ui.components import build_event_rows

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PAGE = str(_REPO_ROOT / "pages" / "Analytics.py")

TOKEN = "tok-9d41e07c2b6f83a5-not-a-real-credential"  # noqa: S105 - a test fixture, not a credential
_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_SINCE = _NOW - timedelta(hours=24)

_COUNTS_BODY = {
    "camera_id": None,
    "since": _SINCE.isoformat(),
    "until": _NOW.isoformat(),
    "counts": {
        "incoming": {"car": 12, "motorcycle": 3, "bus": 1, "truck": 0, "bicycle": 0},
        "outgoing": {"car": 9, "truck": 2},
    },
    "total": 27,
}
_EMPTY_COUNTS_BODY = {
    **_COUNTS_BODY,
    "counts": {"incoming": {}, "outgoing": {}},
    "total": 0,
}
_HOURLY_BODY = {
    "camera_id": None,
    "since": _SINCE.isoformat(),
    "until": _NOW.isoformat(),
    "buckets": [
        {"hour": "2026-10-01T10:00:00+00:00", "total": 14},
        {"hour": "2026-10-01T11:00:00+00:00", "total": 13},
    ],
}


def _event_json(*, crossed_at: datetime, plate: str | None = None) -> dict[str, object]:
    return {
        "camera_id": "toll-plaza-a",
        "track_id": 7,
        "vehicle_class": "bus",
        "direction": "outgoing",
        "crossed_at": crossed_at.isoformat(),
        "confidence": 0.82,
        "plate_text": plate,
        "plate_confidence": None,
    }


def _client(handler, *, token: str | None = None) -> WorkerClient:
    return WorkerClient(
        "http://worker:8000/api", transport=httpx.MockTransport(handler), api_token=token
    )


def _readyz_body(*, lost: int | None = 0) -> dict[str, object]:
    return {
        "ready": True,
        "redis": True,
        "cameras_running": 2,
        "cameras_total": 2,
        "detail": None,
        "database": True,
        "history_events_lost": lost,
    }


def _healthy_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/readyz":
        return httpx.Response(200, json=_readyz_body())
    if path == "/api/history/counts":
        return httpx.Response(200, json=_COUNTS_BODY)
    if path == "/api/history/hourly":
        return httpx.Response(200, json=_HOURLY_BODY)
    if path == "/api/history/events":
        return httpx.Response(200, json=[_event_json(crossed_at=datetime.now(UTC))])
    raise AssertionError(f"unexpected path {path}")


def _counts(body: dict[str, object] = _COUNTS_BODY) -> HistoryCounts:
    return HistoryCounts.model_validate(body)


# --- selectors ----------------------------------------------------------------


def test_camera_options_lead_with_all_cameras_then_the_registry() -> None:
    assert camera_options() == [ALL_CAMERAS, *(c.camera_id for c in CAMERAS)]


def test_camera_label_uses_registry_names() -> None:
    assert camera_label(ALL_CAMERAS) == ALL_CAMERAS
    assert camera_label(CAMERAS[0].camera_id) == CAMERAS[0].name


def test_resolve_camera_all_cameras_means_no_filter() -> None:
    assert resolve_camera(ALL_CAMERAS) is None


def test_resolve_camera_passes_a_known_id_through() -> None:
    assert resolve_camera(CAMERAS[1].camera_id) == CAMERAS[1].camera_id


@pytest.mark.parametrize("hostile", ["not-a-camera", "toll-plaza-a&limit=1", "../etc", "", 7, None])
def test_resolve_camera_never_passes_an_unknown_value_on(hostile: object) -> None:
    assert resolve_camera(hostile) is None


def test_window_bounds_matches_each_label() -> None:
    for label, delta in WINDOWS.items():
        since, until = window_bounds(label, now=_NOW)
        assert until == _NOW
        assert until - since == delta


@pytest.mark.parametrize("bogus", ["Last decade", "", None, 3, ["Last hour"]])
def test_window_bounds_unknown_label_falls_back_to_the_default(bogus: object) -> None:
    since, until = window_bounds(bogus, now=_NOW)
    assert until - since == WINDOWS[DEFAULT_WINDOW]


# --- frames -------------------------------------------------------------------


def test_counts_frame_is_direction_by_class_in_canonical_order() -> None:
    frame = counts_frame(_counts())

    assert frame["class"] == ["car", "motorcycle", "bus", "truck", "bicycle"]
    assert frame["incoming"] == [12, 3, 1, 0, 0]
    # `outgoing` omits motorcycle/bus/bicycle in the payload: no crossings of them.
    assert frame["outgoing"] == [9, 0, 0, 2, 0]


def test_counts_frame_accounts_for_every_crossing_in_the_payload() -> None:
    frame = counts_frame(_counts())
    assert sum(frame["incoming"]) + sum(frame["outgoing"]) == _counts().total  # type: ignore[arg-type]


def test_counts_frame_appends_classes_beyond_the_known_ones() -> None:
    body = {**_COUNTS_BODY, "counts": {"incoming": {"car": 1, "tuk-tuk": 4}}, "total": 5}
    frame = counts_frame(_counts(body))

    assert frame["class"][-1] == "tuk-tuk"
    assert frame["incoming"][-1] == 4
    assert frame["outgoing"] == [0, 0, 0, 0, 0, 0]


def test_direction_totals() -> None:
    assert direction_totals(_counts()) == {Direction.INCOMING: 16, Direction.OUTGOING: 11}


def test_hourly_frame_is_time_ordered_and_does_not_fill_gaps() -> None:
    hourly = HistoryHourly.model_validate(
        {
            **_HOURLY_BODY,
            "buckets": [
                {"hour": "2026-10-01T11:00:00+00:00", "total": 5},
                {"hour": "2026-10-01T08:00:00+00:00", "total": 2},
            ],
        }
    )
    frame = hourly_frame(hourly)

    assert [h.hour for h in frame["hour"]] == [8, 11]  # type: ignore[attr-defined]
    assert frame["crossings"] == [2, 5]


# --- event rows ---------------------------------------------------------------


def _event(*, plate: str | None, crossed_at: datetime = _NOW) -> CrossingEvent:
    return CrossingEvent.model_validate(_event_json(crossed_at=crossed_at, plate=plate))


def test_event_rows_show_a_dash_when_no_plate_was_read() -> None:
    (row,) = build_event_rows([_event(plate=None)])
    assert row["plate"] == "—"


def test_event_rows_show_a_plate_only_when_one_was_read() -> None:
    (row,) = build_event_rows([_event(plate="DHAKA-METRO-GA-11-2233")])
    assert row["plate"] == "DHAKA-METRO-GA-11-2233"


def test_event_rows_render_times_in_utc_with_a_caller_chosen_format() -> None:
    dhaka = timezone(timedelta(hours=6))
    crossed_at = datetime(2026, 10, 1, 18, 5, 9, tzinfo=dhaka)  # 12:05:09 UTC

    (live,) = build_event_rows([_event(plate=None, crossed_at=crossed_at)])
    (history,) = build_event_rows(
        [_event(plate=None, crossed_at=crossed_at)], time_format="%Y-%m-%d %H:%M:%S"
    )

    assert live["time"] == "12:05:09"
    assert history["time"] == "2026-10-01 12:05:09"


# --- fetch_history: four outcomes, kept distinct ------------------------------


def _fetch(client: WorkerClient) -> analytics.HistoryView:
    return fetch_history(client, camera_id=None, since=_SINCE, until=_NOW)


def test_fetch_history_ok() -> None:
    view = _fetch(_client(_healthy_handler))

    assert view.status is HistoryStatus.OK
    assert view.counts is not None
    assert view.counts.total == 27
    assert view.hourly is not None
    assert len(view.events) == 1


def test_fetch_history_empty_window_is_a_real_result_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/history/counts":
            return httpx.Response(200, json=_EMPTY_COUNTS_BODY)
        if request.url.path == "/api/history/hourly":
            return httpx.Response(200, json={**_HOURLY_BODY, "buckets": []})
        return httpx.Response(200, json=[])

    view = _fetch(_client(handler))

    assert view.status is HistoryStatus.EMPTY
    assert view.message == ""
    assert view.counts is not None
    assert view.counts.total == 0


def test_fetch_history_503_is_unavailable_not_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "history is not enabled"})

    view = _fetch(_client(handler))

    assert view.status is HistoryStatus.UNAVAILABLE
    assert view.counts is None
    assert "unavailable" in view.message.lower()


def test_fetch_history_401_is_unauthorized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid or missing API token"})

    view = _fetch(_client(handler))

    assert view.status is HistoryStatus.UNAUTHORIZED
    assert "check TRAFFIC_AI_API_TOKEN" in view.message


def test_fetch_history_connection_error_is_a_generic_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    view = _fetch(_client(handler))

    assert view.status is HistoryStatus.ERROR
    assert "could not reach worker" in view.message


def test_fetch_history_filters_the_events_table_to_the_window() -> None:
    inside = _NOW - timedelta(hours=1)
    outside = _SINCE - timedelta(minutes=1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/history/events":
            return httpx.Response(
                200, json=[_event_json(crossed_at=inside), _event_json(crossed_at=outside)]
            )
        return _healthy_handler(request)

    view = _fetch(_client(handler))

    assert [e.crossed_at for e in view.events] == [inside]


def test_fetch_history_only_empty_when_nothing_in_the_window_at_all() -> None:
    # counts say zero but a windowed event exists (e.g. the batch writer flushed
    # between the two calls): that is not "no crossings", so do not claim it.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/history/counts":
            return httpx.Response(200, json=_EMPTY_COUNTS_BODY)
        return _healthy_handler(request)

    handler_events = [_event_json(crossed_at=_NOW - timedelta(minutes=5))]

    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/history/events":
            return httpx.Response(200, json=handler_events)
        return handler(request)

    assert _fetch(_client(wrapped)).status is HistoryStatus.OK


# --- the rendered page --------------------------------------------------------


def _install(monkeypatch: pytest.MonkeyPatch, handler, *, token: str | None = None):
    """Route the page's `get_worker_client()` to a real client over a mock
    transport, recording every request it makes."""
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = _client(recording, token=token)
    monkeypatch.setattr(analytics, "get_worker_client", lambda: client)
    return requests


def _run() -> AppTest:
    at = AppTest.from_file(_PAGE)
    at.run(timeout=15)
    return at


def _all_text(at: AppTest) -> str:
    """Every string the page put on screen, for credential-leak assertions."""
    parts: list[str] = [str(at.main)]
    for element in (*at.markdown, *at.error, *at.warning, *at.info, *at.caption, *at.metric):
        parts.append(str(element.value))
    return "\n".join(parts)


def test_page_renders_history_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _healthy_handler)
    at = _run()

    assert len(at.exception) == 0
    assert [t.value for t in at.title] == ["Analytics"]
    assert at.selectbox(key="analytics:camera").value == ALL_CAMERAS
    assert at.selectbox(key="analytics:window").value == DEFAULT_WINDOW
    assert [m.label for m in at.metric] == ["Total crossings", "Incoming", "Outgoing"]
    assert [m.value for m in at.metric] == ["27", "16", "11"]
    assert len(at.error) == 0
    assert len(at.warning) == 0
    assert not any("No crossings recorded" in i.value for i in at.info)
    # Two charts (class bar chart, hourly line chart) and two tables. AppTest has no
    # typed accessor for charts; both built-ins surface as `vega_lite_chart`.
    assert len(at.get("vega_lite_chart")) == 2
    assert len(at.dataframe) == 2


def test_page_shows_a_dash_for_an_unread_plate(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _healthy_handler)
    at = _run()

    events_table = at.dataframe[1].value
    assert list(events_table["plate"]) == ["—"]


def test_page_says_so_when_the_window_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/history/counts":
            return httpx.Response(200, json=_EMPTY_COUNTS_BODY)
        if request.url.path == "/api/history/hourly":
            return httpx.Response(200, json={**_HOURLY_BODY, "buckets": []})
        return httpx.Response(200, json=[])

    _install(monkeypatch, handler)
    at = _run()

    assert len(at.exception) == 0
    assert [i.value for i in at.info] == ["No crossings recorded in this window."]
    assert len(at.error) == 0
    assert len(at.warning) == 0
    assert len(at.dataframe) == 0


def test_page_says_history_is_unavailable_on_503(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, lambda request: httpx.Response(503, json={"detail": "off"}))
    at = _run()

    assert len(at.exception) == 0
    assert any("History unavailable" in w.value for w in at.warning)
    # Unavailable is not "empty": the page must not claim there were no crossings.
    assert not any("No crossings recorded" in i.value for i in at.info)
    assert len(at.dataframe) == 0


def test_page_says_the_ui_is_unauthorized_on_401_and_never_shows_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": f"bad token {TOKEN}"})

    _install(monkeypatch, handler, token=TOKEN)
    at = _run()

    assert len(at.exception) == 0
    assert any("not authorized to the API" in e.value for e in at.error)
    assert any("TRAFFIC_AI_API_TOKEN" in e.value for e in at.error)
    assert TOKEN not in _all_text(at)


def test_page_reports_a_load_failure_visibly(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _install(monkeypatch, handler, token=TOKEN)
    at = _run()

    assert len(at.exception) == 0
    assert any("Could not load history" in e.value for e in at.error)
    assert TOKEN not in _all_text(at)


def test_page_never_shows_the_token_when_history_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _install(monkeypatch, _healthy_handler, token=TOKEN)
    at = _run()

    assert {r.headers["authorization"] for r in requests} == {f"Bearer {TOKEN}"}
    assert TOKEN not in _all_text(at)


def test_all_cameras_sends_no_camera_filter_and_a_selected_camera_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install(monkeypatch, _healthy_handler)
    at = _run()

    assert requests, "the page made no API calls"
    assert all("camera_id" not in r.url.params for r in requests)

    requests.clear()
    at.selectbox(key="analytics:camera").select(CAMERAS[1].camera_id).run(timeout=15)

    assert len(at.exception) == 0
    # The readiness lookup behind the "totals may be incomplete" warning is not a history
    # query and carries no camera filter, so only the `/history/*` requests are checked.
    history = [r for r in requests if r.url.path.startswith("/api/history/")]
    assert history
    assert {r.url.params["camera_id"] for r in history} == {CAMERAS[1].camera_id}


def test_window_selector_changes_the_requested_range(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _install(monkeypatch, _healthy_handler)
    at = _run()
    at.selectbox(key="analytics:window").select("Last hour").run(timeout=15)

    assert len(at.exception) == 0
    counts_request = [r for r in requests if r.url.path == "/api/history/counts"][-1]
    since = datetime.fromisoformat(counts_request.url.params["since"])
    until = datetime.fromisoformat(counts_request.url.params["until"])
    assert until - since == timedelta(hours=1)


def test_page_state_is_namespaced_so_it_cannot_collide_with_the_other_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _healthy_handler)
    at = _run()

    assert set(at.session_state.to_dict()) == {"analytics:camera", "analytics:window"}


# --- lost history events: totals that may be short must say so -------------------


def _with_readyz(readyz: object):
    """`_healthy_handler`, but `/readyz` answers with `readyz` (or raises if it is an
    exception), so a test controls only what the lost-events lookup sees."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/readyz":
            if isinstance(readyz, Exception):
                raise readyz
            return httpx.Response(200, json=readyz)
        return _healthy_handler(request)

    return handler


def test_fetch_history_carries_the_lost_event_count() -> None:
    view = _fetch(_client(_with_readyz(_readyz_body(lost=3))))

    assert view.status is HistoryStatus.OK
    assert view.events_lost == 3


def test_fetch_history_has_no_lost_count_when_the_worker_did_not_report_one() -> None:
    assert _fetch(_client(_with_readyz(_readyz_body(lost=None)))).events_lost is None


def test_page_warns_that_totals_may_be_incomplete_when_events_were_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _with_readyz(_readyz_body(lost=4)))
    at = _run()

    assert len(at.exception) == 0
    (warning,) = at.warning
    assert "4 crossing(s)" in warning.value
    assert "may be incomplete" in warning.value
    # The totals are still shown: the warning qualifies them, it does not replace them.
    assert [m.value for m in at.metric] == ["27", "16", "11"]


def test_page_warns_on_an_empty_window_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty window is the case where lost events most plausibly explain what is on
    screen, so "no crossings" must not stand unqualified."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/readyz":
            return httpx.Response(200, json=_readyz_body(lost=2))
        if request.url.path == "/api/history/counts":
            return httpx.Response(200, json=_EMPTY_COUNTS_BODY)
        if request.url.path == "/api/history/hourly":
            return httpx.Response(200, json={**_HOURLY_BODY, "buckets": []})
        return httpx.Response(200, json=[])

    _install(monkeypatch, handler)
    at = _run()

    assert [i.value for i in at.info] == ["No crossings recorded in this window."]
    assert any("may be incomplete" in w.value for w in at.warning)


@pytest.mark.parametrize("lost", [0, None])
def test_page_does_not_warn_when_nothing_was_lost_or_nothing_was_reported(
    monkeypatch: pytest.MonkeyPatch, lost: int | None
) -> None:
    _install(monkeypatch, _with_readyz(_readyz_body(lost=lost)))
    at = _run()

    assert len(at.warning) == 0


def test_page_still_renders_history_when_readiness_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warning is advisory: failing to fetch it must not blank a page that loaded."""
    request_failure = httpx.ConnectError("connection refused")
    _install(monkeypatch, _with_readyz(request_failure))
    at = _run()

    assert len(at.exception) == 0
    assert len(at.error) == 0
    assert [m.value for m in at.metric] == ["27", "16", "11"]
    assert len(at.warning) == 0


# --- demo footage disclosure ------------------------------------------------------


def test_page_discloses_that_the_footage_is_looped_demo_footage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _healthy_handler)
    at = _run()

    captions = [c.value for c in at.caption]
    assert any("looped" in c and "not real traffic" in c for c in captions)


def test_the_disclosure_is_shown_even_when_history_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, lambda request: httpx.Response(503, json={"detail": "off"}))
    at = _run()

    assert any("looped" in c.value for c in at.caption)
