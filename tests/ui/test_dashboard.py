"""End-to-end smoke tests for the two entrypoints via `AppTest`.

No worker is running, so the real `httpx.Client` inside `WorkerClient` fails
to connect — this is exactly the "worker completely down" case the packet
requires to render usefully: a banner, a still-drawn map, and no traceback.

The 401 tests install a real `WorkerClient` over `httpx.MockTransport` in place of
`get_worker_client`, as `test_analytics.py` does: the AppTest script runs in this
process, so the patch holds and the genuine client code decides what is raised.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

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
