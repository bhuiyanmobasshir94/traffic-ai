"""End-to-end smoke tests for the two entrypoints via `AppTest`.

No worker is running, so the real `httpx.Client` inside `WorkerClient` fails
to connect — this is exactly the "worker completely down" case the packet
requires to render usefully: a banner, a still-drawn map, and no traceback.
"""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

_REPO_ROOT = Path(__file__).resolve().parents[2]


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
