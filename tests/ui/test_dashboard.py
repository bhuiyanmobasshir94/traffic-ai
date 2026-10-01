"""End-to-end smoke tests for the two entrypoints via `AppTest`.

No worker is running, so the real `httpx.Client` inside `WorkerClient` fails
to connect — this is exactly the "worker completely down" case the packet
requires to render usefully: a banner, a still-drawn map, and no traceback.

The 401 tests install a real `WorkerClient` over `httpx.MockTransport` in place of
`get_worker_client`, as `test_analytics.py` does: the AppTest script runs in this
process, so the patch holds and the genuine client code decides what is raised.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from traffic_ai.cameras import CAMERAS, DEFAULT_COUNTING_DISABLED_REASON
from traffic_ai.ui import dashboard
from traffic_ai.ui.client import WorkerClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PAGES = {
    "Toll Booth": str(_REPO_ROOT / "Toll_Booth.py"),
    "Traffic Analysis": str(_REPO_ROOT / "pages" / "Traffic_Analysis.py"),
}

TOKEN = "tok-5b1c7d90e2a4f368-not-a-real-credential"  # noqa: S105 - a test fixture, not a credential


def test_toll_booth_renders_without_exception_when_worker_is_down() -> None:
    at = AppTest.from_file(str(_REPO_ROOT / "Toll_Booth.py"))
    at.run(timeout=15)

    assert len(at.exception) == 0
    assert [t.value for t in at.title] == ["Toll Booth"]
    assert any("Worker unreachable" in e.value for e in at.error)


def test_traffic_analysis_renders_without_exception_when_worker_is_down() -> None:
    at = AppTest.from_file(str(_REPO_ROOT / "pages" / "Traffic_Analysis.py"))
    at.run(timeout=15)

    assert len(at.exception) == 0
    assert [t.value for t in at.title] == ["Traffic Analysis"]
    assert any("Worker unreachable" in e.value for e in at.error)


# --- the demo-footage disclosure ----------------------------------------------------


@pytest.mark.parametrize("page", _PAGES)
def test_the_live_pages_disclose_that_the_footage_is_looped(page: str) -> None:
    at = AppTest.from_file(_PAGES[page])
    at.run(timeout=15)

    assert len(at.exception) == 0
    assert any("looped" in c.value and "not real traffic" in c.value for c in at.caption)


# --- 401 is "not authorized", never "worker unreachable" ----------------------------


def _only_the_probe_is_open(request: httpx.Request) -> httpx.Response:
    """A worker with auth on and a UI holding the wrong token: `/healthz` is exempt
    from auth, everything else is a 401 -- the combination that used to read as an
    outage."""
    if request.url.path == "/api/healthz":
        return httpx.Response(200, json={"status": "ok"})
    return httpx.Response(401, json={"detail": "Unauthorized"})


@pytest.mark.parametrize("page", _PAGES)
def test_a_401_from_the_worker_says_not_authorized_not_unreachable(
    monkeypatch: pytest.MonkeyPatch, page: str
) -> None:
    client = WorkerClient(
        "http://worker:8000/api",
        transport=httpx.MockTransport(_only_the_probe_is_open),
        api_token=TOKEN,
    )
    monkeypatch.setattr(dashboard, "get_worker_client", lambda: client)

    at = AppTest.from_file(_PAGES[page])
    at.run(timeout=15)

    assert len(at.exception) == 0
    errors = [e.value for e in at.error]
    assert any("not authorized" in e and "TRAFFIC_AI_API_TOKEN" in e for e in errors)
    assert not any("unreachable" in e.lower() for e in errors)
    # The map and the rest of the page still render, and the token is never shown.
    assert TOKEN not in "\n".join([str(at.main), *errors])


# --- a camera whose counting is switched off ---------------------------------------------
#
# The worker publishes live state for it, but nothing in that state is a measurement of
# crossings. The state below carries numbers a counting camera would show (a rate, a
# congestion level, 5 cars counted), to prove the page does not render them.

_CALIBRATED = next(c for c in CAMERAS if c.counting_enabled)
_UNCALIBRATED = next(c for c in CAMERAS if not c.counting_enabled)
_COUNTING_METRICS = {"Throughput / min", "Congestion", "Total counted"}


def _published_state(camera_id: str, name: str, *, counting_enabled: bool) -> dict[str, object]:
    return {
        "camera_id": camera_id,
        "name": name,
        "status": "running",
        "updated_at": datetime.now(UTC).isoformat(),
        "counts": {"incoming": {"car": 5}, "outgoing": {}},
        "active_tracks": 7,
        "throughput_per_min": 99.0,
        "congestion": "heavy",
        "counting_enabled": counting_enabled,
    }


def _install_worker(
    monkeypatch: pytest.MonkeyPatch, *, uncalibrated_publishes_counting_enabled: bool
) -> list[str]:
    """A healthy worker with both cameras publishing state. Returns the request paths."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paths.append(path)
        if path == "/api/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/api/cameras":
            return httpx.Response(
                200,
                json=[
                    {
                        "camera_id": c.camera_id,
                        "name": c.name,
                        "latitude": c.latitude,
                        "longitude": c.longitude,
                    }
                    for c in CAMERAS
                ],
            )
        for camera in CAMERAS:
            if path == f"/api/cameras/{camera.camera_id}/state":
                enabled = camera.counting_enabled or uncalibrated_publishes_counting_enabled
                return httpx.Response(
                    200,
                    json=_published_state(camera.camera_id, camera.name, counting_enabled=enabled),
                )
            if path == f"/api/cameras/{camera.camera_id}/events":
                return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected path {path}")

    client = WorkerClient("http://worker:8000/api", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(dashboard, "get_worker_client", lambda: client)
    return paths


def _page_text(at: AppTest) -> str:
    parts = [str(at.main)]
    for element in (*at.markdown, *at.error, *at.warning, *at.info, *at.caption, *at.metric):
        parts.append(str(element.value))
    return "\n".join(parts)


@pytest.mark.parametrize("published_counting_enabled", [False, True])
def test_the_dashboard_shows_the_reason_not_the_metrics_for_an_uncalibrated_camera(
    monkeypatch: pytest.MonkeyPatch, published_counting_enabled: bool
) -> None:
    # `True` is a state from a worker that predates the flag: the registry still wins.
    paths = _install_worker(
        monkeypatch, uncalibrated_publishes_counting_enabled=published_counting_enabled
    )
    at = AppTest.from_file(_PAGES["Traffic Analysis"])  # defaults to the uncalibrated camera
    at.run(timeout=15)

    assert len(at.exception) == 0
    assert _UNCALIBRATED.camera_id == at.session_state["traffic_analysis:selected_camera_id"]
    assert [i.value for i in at.info] == [_UNCALIBRATED.counting_disabled_reason]
    # What is still real is still shown: the pipeline is live and tracks are real.
    assert any("Live" in s.value for s in at.success)
    assert {m.label: m.value for m in at.metric} == {"Active tracks": "7"}
    # None of the counting-derived numbers, nor any table that could hold them.
    assert not _COUNTING_METRICS & {m.label for m in at.metric}
    assert len(at.dataframe) == 0
    text = _page_text(at)
    assert "Heavy" not in text
    assert "99.0" not in text
    assert not any("No crossings recorded" in c.value for c in at.caption)
    # Its (always empty) event list is not fetched: an empty table would read as "none".
    assert f"/api/cameras/{_UNCALIBRATED.camera_id}/events" not in paths


def test_the_dashboard_still_shows_the_metrics_for_a_camera_that_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _install_worker(monkeypatch, uncalibrated_publishes_counting_enabled=False)
    at = AppTest.from_file(_PAGES["Toll Booth"])  # defaults to the calibrated camera
    at.run(timeout=15)

    assert len(at.exception) == 0
    assert _CALIBRATED.camera_id == at.session_state["toll_booth:selected_camera_id"]
    assert {m.label: m.value for m in at.metric} == {
        "Throughput / min": "99.0",
        "Active tracks": "7",
        "Congestion": "Heavy",
        "Total counted": "5",
    }
    assert not any("not calibrated" in i.value for i in at.info)
    assert f"/api/cameras/{_CALIBRATED.camera_id}/events" in paths


def test_a_409_from_the_worker_fails_closed_for_a_camera_the_registry_thinks_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A UI older than its worker: the registry says camera A counts, the worker's events
    route says it does not. The stricter opinion wins, and it is not "worker unreachable"."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/api/cameras":
            return httpx.Response(200, json=[])
        if path == f"/api/cameras/{_CALIBRATED.camera_id}/state":
            return httpx.Response(
                200,
                json=_published_state(
                    _CALIBRATED.camera_id, _CALIBRATED.name, counting_enabled=True
                ),
            )
        if path == f"/api/cameras/{_CALIBRATED.camera_id}/events":
            return httpx.Response(409, json={"detail": "counting is not calibrated"})
        raise AssertionError(f"unexpected path {path}")

    client = WorkerClient("http://worker:8000/api", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(dashboard, "get_worker_client", lambda: client)
    at = AppTest.from_file(_PAGES["Toll Booth"])
    at.run(timeout=15)

    assert len(at.exception) == 0
    assert [i.value for i in at.info] == [DEFAULT_COUNTING_DISABLED_REASON]
    assert not any("unreachable" in e.value.lower() for e in at.error)
    assert not _COUNTING_METRICS & {m.label for m in at.metric}
    assert len(at.dataframe) == 0
    assert "Heavy" not in _page_text(at)


def test_an_uncalibrated_camera_shows_its_reason_even_before_it_has_published_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/cameras":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": "no state yet"})

    client = WorkerClient("http://worker:8000/api", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(dashboard, "get_worker_client", lambda: client)
    at = AppTest.from_file(_PAGES["Traffic Analysis"])
    at.run(timeout=15)

    assert len(at.exception) == 0
    assert [i.value for i in at.info if "not calibrated" in i.value] == [
        _UNCALIBRATED.counting_disabled_reason
    ]
    assert len(at.metric) == 0


def test_an_unreachable_worker_still_says_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 401 branch must not swallow the real outage: a worker that answers its probe and
    then fails everything else with a 500 is still reported as a failure, not as auth."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/healthz":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(500, json={"detail": "boom"})

    client = WorkerClient("http://worker:8000/api", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(dashboard, "get_worker_client", lambda: client)

    at = AppTest.from_file(_PAGES["Toll Booth"])
    at.run(timeout=15)

    assert len(at.exception) == 0
    errors = [e.value for e in at.error]
    assert any("Worker unreachable" in e for e in errors)
    assert not any("not authorized" in e for e in errors)
