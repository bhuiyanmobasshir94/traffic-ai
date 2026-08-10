"""Tests for the pure-logic pieces of `traffic_ai.ui.components`.

The rendering calls themselves (`st.markdown`, `st.dataframe`, ...) need a
Streamlit script context to fully exercise; the injection boundary and the
click-allowlist behaviour do not, so they are pulled into small pure
functions (`_build_stream_markup`, `resolve_click`) specifically so they are
testable here.
"""

from __future__ import annotations

from traffic_ai.cameras import CAMERAS
from traffic_ai.ui.components import _build_stream_markup, resolve_click


def test_build_stream_markup_unknown_camera_returns_none() -> None:
    assert _build_stream_markup("not-a-camera", "/api") is None


def test_build_stream_markup_known_camera_builds_url() -> None:
    camera = CAMERAS[0]
    markup = _build_stream_markup(camera.camera_id, "/api")
    assert markup is not None
    assert f"/api/cameras/{camera.camera_id}/stream.mjpg" in markup
    assert markup.startswith("<img ")


def test_build_stream_markup_escapes_untrusted_base_url() -> None:
    # `public_base_url` ultimately comes from `Settings.api_public_url` — not
    # user input today, but the injection boundary must hold regardless of
    # where the string originates (`.claude/rules/streamlit-app.md`).
    camera = CAMERAS[0]
    hostile = '"><script>alert(1)</script>'
    markup = _build_stream_markup(camera.camera_id, hostile)
    assert markup is not None
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


def test_resolve_click_none_falls_back() -> None:
    assert resolve_click(None, "toll-plaza-a") == "toll-plaza-a"


def test_resolve_click_unknown_popup_falls_back() -> None:
    assert resolve_click("not-a-real-camera-id", "toll-plaza-a") == "toll-plaza-a"


def test_resolve_click_non_string_popup_falls_back() -> None:
    assert resolve_click(123, "toll-plaza-a") == "toll-plaza-a"


def test_resolve_click_known_popup_resolves() -> None:
    camera = CAMERAS[1]
    assert resolve_click(camera.camera_id, CAMERAS[0].camera_id) == camera.camera_id
