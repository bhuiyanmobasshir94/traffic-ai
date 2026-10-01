"""`python -m traffic_ai.api` -- how uvicorn is started."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest


def _entrypoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    """The `__main__` module, imported with no stray `.env` and no inherited production flags.

    Importing it builds the app (`create_app()`), which reads `Settings` from the environment
    and from a `.env` in the working directory -- neither belongs in this test.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRAFFIC_AI_ENVIRONMENT", raising=False)
    monkeypatch.delenv("TRAFFIC_AI_API_TOKEN", raising=False)
    return importlib.import_module("traffic_ai.api.__main__")


def test_uvicorn_is_started_with_a_bounded_graceful_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without `timeout_graceful_shutdown` uvicorn waits forever for open requests, and an MJPEG
    stream on a healthy pipeline never ends: one open tab holds the container until it is
    SIGKILLed, and the lifespan teardown that drains the history writer never runs."""
    entry = _entrypoint(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(entry.uvicorn, "run", fake_run)
    monkeypatch.setenv("TRAFFIC_AI_API_HOST", "127.0.0.1")
    monkeypatch.setenv("TRAFFIC_AI_API_PORT", "9123")

    entry.main()

    assert captured["timeout_graceful_shutdown"] == 5
    assert entry.GRACEFUL_SHUTDOWN_SECONDS == 5
    assert captured["app"] is entry.app
    assert (captured["host"], captured["port"]) == ("127.0.0.1", 9123)
