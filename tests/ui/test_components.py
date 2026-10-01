"""Tests for the pure-logic pieces of `traffic_ai.ui.components`.

The rendering calls themselves (`st.markdown`, `st.dataframe`, ...) need a
Streamlit script context to fully exercise; the injection boundary and the
click-allowlist behaviour do not, so they are pulled into small pure
functions (`_build_stream_markup`, `resolve_click`) specifically so they are
testable here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import folium
import pytest

from traffic_ai.cameras import CAMERAS, ROAD_SEGMENTS, get_camera
from traffic_ai.domain import CameraState, CongestionLevel, PipelineStatus
from traffic_ai.ui import components
from traffic_ai.ui.components import (
    _build_stream_markup,
    _corridor_color,
    counting_disabled_reason,
    render_map,
    resolve_click,
)

_CALIBRATED = next(c for c in CAMERAS if c.counting_enabled)
_UNCALIBRATED = next(c for c in CAMERAS if not c.counting_enabled)


def _state(camera_id: str, *, congestion: CongestionLevel, counting_enabled: bool = True):
    return CameraState(
        camera_id=camera_id,
        name=camera_id,
        status=PipelineStatus.RUNNING,
        updated_at=datetime.now(UTC),
        congestion=congestion,
        counting_enabled=counting_enabled,
    )


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


# --- uncalibrated cameras: reason and map colour -----------------------------------------


def test_counting_disabled_reason_is_none_for_a_camera_that_counts() -> None:
    assert counting_disabled_reason(_CALIBRATED.camera_id) is None
    state = _state(_CALIBRATED.camera_id, congestion=CongestionLevel.HEAVY)
    assert counting_disabled_reason(_CALIBRATED.camera_id, state) is None


def test_counting_disabled_reason_comes_from_the_registry() -> None:
    assert counting_disabled_reason(_UNCALIBRATED.camera_id) == (
        _UNCALIBRATED.counting_disabled_reason
    )


def test_counting_disabled_reason_fails_closed_when_either_side_says_off() -> None:
    # The registry says off but a state (e.g. from an older worker) says on: still off.
    on = _state(_UNCALIBRATED.camera_id, congestion=CongestionLevel.HEAVY)
    assert counting_disabled_reason(_UNCALIBRATED.camera_id, on) == (
        _UNCALIBRATED.counting_disabled_reason
    )
    # The published state says off but the registry says on: off, with a generic reason
    # rather than none and rather than the numbers.
    off = _state(_CALIBRATED.camera_id, congestion=CongestionLevel.HEAVY, counting_enabled=False)
    reason = counting_disabled_reason(_CALIBRATED.camera_id, off)
    assert reason is not None
    assert "not calibrated" in reason


def test_counting_disabled_reason_for_an_unknown_id_follows_the_state_alone() -> None:
    assert counting_disabled_reason("not-a-camera") is None
    off = _state("not-a-camera", congestion=CongestionLevel.FREE_FLOW, counting_enabled=False)
    assert counting_disabled_reason("not-a-camera", off) is not None


def test_corridor_color_is_the_congestion_colour_for_a_camera_that_counts() -> None:
    state = _state(_CALIBRATED.camera_id, congestion=CongestionLevel.HEAVY)
    assert _corridor_color(_CALIBRATED.camera_id, state) == CongestionLevel.HEAVY.map_color


@pytest.mark.parametrize("published_counting_enabled", [False, True])
def test_corridor_color_is_the_fallback_for_an_uncalibrated_camera(
    published_counting_enabled: bool,
) -> None:
    # A congestion level that would be coloured darkred if it were trusted.
    state = _state(
        _UNCALIBRATED.camera_id,
        congestion=CongestionLevel.STANDSTILL,
        counting_enabled=published_counting_enabled,
    )
    assert _corridor_color(_UNCALIBRATED.camera_id, state) == components._FALLBACK_COLOR
    assert CongestionLevel.STANDSTILL.map_color != components._FALLBACK_COLOR


def test_corridor_color_is_the_fallback_without_a_state() -> None:
    assert _corridor_color(_CALIBRATED.camera_id, None) == components._FALLBACK_COLOR


def _drawn_colors(
    monkeypatch: pytest.MonkeyPatch, states: dict[str, CameraState]
) -> tuple[dict[str, str], dict[str, str]]:
    """Run the real `render_map` with `st_folium` stubbed to capture the folium map, and
    return (marker colour by camera_id, corridor colour by segment name) as drawn."""
    captured: list[folium.Map] = []

    def fake_st_folium(fmap: folium.Map, **_: object) -> dict[str, object]:
        captured.append(fmap)
        return {}

    monkeypatch.setattr(components, "st_folium", fake_st_folium)
    render_map(states, key="test-map")
    (fmap,) = captured

    markers: dict[str, str] = {}
    corridors: dict[str, str] = {}
    for child in fmap._children.values():
        if isinstance(child, folium.Marker):
            camera = next(
                c
                for c in CAMERAS
                if list(child.location) == [c.latitude, c.longitude]  # type: ignore[arg-type]
            )
            markers[camera.camera_id] = child.icon.options["marker_color"]
        elif isinstance(child, folium.PolyLine):
            segment = next(s for s in ROAD_SEGMENTS if [list(p) for p in child.locations] == s.path)
            corridors[segment.name] = child.options["color"]
    return markers, corridors


def test_the_map_colours_a_counting_camera_by_congestion_and_an_uncalibrated_one_gray(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = {
        _CALIBRATED.camera_id: _state(_CALIBRATED.camera_id, congestion=CongestionLevel.HEAVY),
        # Published numbers that would paint the corridor darkred if they were believed.
        _UNCALIBRATED.camera_id: _state(
            _UNCALIBRATED.camera_id, congestion=CongestionLevel.STANDSTILL, counting_enabled=False
        ),
    }

    markers, corridors = _drawn_colors(monkeypatch, states)

    assert markers[_CALIBRATED.camera_id] == "red"
    assert markers[_UNCALIBRATED.camera_id] == components._FALLBACK_COLOR
    for segment in ROAD_SEGMENTS:
        owner = get_camera(segment.camera_id)
        assert owner is not None
        expected = "red" if owner.counting_enabled else components._FALLBACK_COLOR
        assert corridors[segment.name] == expected, segment.name
    assert "darkred" not in {*markers.values(), *corridors.values()}


def test_the_map_is_all_fallback_when_no_camera_has_published_a_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers, corridors = _drawn_colors(monkeypatch, {})

    assert set(markers.values()) == {components._FALLBACK_COLOR}
    assert set(corridors.values()) == {components._FALLBACK_COLOR}
