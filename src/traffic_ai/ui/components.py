"""Shared rendering pieces, defined exactly once.

The original demo copy-pasted the map block five times and the iframe HTML
six (`CLAUDE.md`, `.claude/rules/streamlit-app.md`). Every page-level piece of
UI lives here instead, so a fix applies everywhere at once.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

import folium
import streamlit as st
from streamlit_folium import st_folium

from traffic_ai.cameras import (
    CAMERAS,
    CAMERAS_BY_ID,
    MAP_CENTER,
    MAP_ZOOM,
    ROAD_SEGMENTS,
    get_camera,
)
from traffic_ai.config import get_settings
from traffic_ai.domain import (
    VEHICLE_CLASSES,
    CameraState,
    ClassCounts,
    CrossingEvent,
    Direction,
    PipelineStatus,
)

# Resolved relative to this module file, not the process working directory —
# `st.sidebar.image("logo.png")` broke whenever the app was launched from
# anywhere but the repository root (CLAUDE.md). `ui/components.py` lives at
# `src/traffic_ai/ui/`, three levels below the repo root.
_LOGO_PATH = Path(__file__).resolve().parents[3] / "logo.png"

_FALLBACK_COLOR = "gray"


def render_sidebar() -> None:
    """Logo and nav chrome. Streamlit's `pages/` convention renders the page
    list itself; nothing here targets its internal selectors."""
    if _LOGO_PATH.exists():
        st.sidebar.image(str(_LOGO_PATH), caption="Graaho Technologies", width=130)
    else:
        st.sidebar.markdown("**Graaho Technologies**")


def _build_stream_markup(camera_id: str, public_base_url: str) -> str | None:
    """Pure helper behind `render_live_video`: returns the escaped `<img>`
    markup for a known camera, or `None` for an unknown one.

    Kept separate from the Streamlit call so the injection boundary — nothing
    user- or config-derived reaches `unsafe_allow_html` unescaped — is unit
    testable without a script context.
    """
    camera = CAMERAS_BY_ID.get(camera_id)
    if camera is None:
        return None
    url = html.escape(
        f"{public_base_url.rstrip('/')}/cameras/{camera.camera_id}/stream.mjpg", quote=True
    )
    alt = html.escape(f"{camera.name} live feed", quote=True)
    return f'<img src="{url}" alt="{alt}" style="width:100%;border-radius:0.5rem;display:block;" />'


def render_live_video(camera_id: str, public_base_url: str) -> None:
    """The annotated MJPEG feed for `camera_id`, streamed directly by the
    browser — Streamlit never touches a frame."""
    markup = _build_stream_markup(camera_id, public_base_url)
    if markup is None:
        st.warning(f"Unknown camera: {camera_id!r}")
        return
    st.markdown(markup, unsafe_allow_html=True)


def render_map(states: dict[str, CameraState], *, key: str = "traffic-map") -> dict[str, Any]:
    """Folium map: one marker per camera, one polyline per road segment,
    each coloured from that segment's/camera's live `CongestionLevel`.

    `key` namespaces the underlying component instance so the toll-booth and
    traffic-analysis pages, which both call this, do not share one widget.

    Returns the `st_folium` click payload for the caller to resolve into a
    camera selection.
    """
    m = folium.Map(location=MAP_CENTER, zoom_start=MAP_ZOOM)

    for camera in CAMERAS:
        state = states.get(camera.camera_id)
        color = state.congestion.map_color if state is not None else _FALLBACK_COLOR
        folium.Marker(
            [camera.latitude, camera.longitude],
            # The popup carries the camera_id, not the display name, so a
            # click resolves through `traffic_ai.cameras.get_camera` — an
            # allowlist lookup, never a raw dict index on click-driven input.
            popup=camera.camera_id,
            tooltip=camera.name,
            icon=folium.Icon(color=color),
        ).add_to(m)

    for segment in ROAD_SEGMENTS:
        state = states.get(segment.camera_id)
        color = state.congestion.map_color if state is not None else _FALLBACK_COLOR
        folium.PolyLine(segment.path, color=color, weight=10, tooltip=segment.name).add_to(m)

    return st_folium(m, use_container_width=True, height=320, key=key)


def render_stats(state: CameraState | None) -> None:
    """Counts by direction and class, throughput, congestion, active tracks."""
    if state is None:
        st.caption("No stats yet.")
        return

    metric_cols = st.columns(4)
    metric_cols[0].metric("Throughput / min", f"{state.throughput_per_min:.1f}")
    metric_cols[1].metric("Active tracks", state.active_tracks)
    metric_cols[2].metric("Congestion", state.congestion.label)
    metric_cols[3].metric("Total counted", state.total_counted)

    direction_cols = st.columns(len(Direction))
    for col, direction in zip(direction_cols, Direction, strict=True):
        counts = state.counts.get(direction, ClassCounts())
        with col:
            st.caption(direction.value.title())
            st.dataframe(
                {
                    "class": list(VEHICLE_CLASSES),
                    "count": [getattr(counts, vehicle_class) for vehicle_class in VEHICLE_CLASSES],
                },
                hide_index=True,
                use_container_width=True,
            )


def render_events(events: list[CrossingEvent]) -> None:
    """Recent crossings table. `plate_text` is `None` whenever ANPR is not
    enabled — that renders as an explicit dash, never an invented plate."""
    if not events:
        st.caption("No crossings recorded yet.")
        return

    rows = [
        {
            "time": event.crossed_at.strftime("%H:%M:%S"),
            "camera": event.camera_id,
            "class": event.vehicle_class,
            "direction": event.direction.value,
            "confidence": f"{event.confidence:.0%}",
            "plate": event.plate_text if event.plate_text is not None else "—",
        }
        for event in events
    ]
    st.dataframe(rows, hide_index=True, use_container_width=True)
    if all(event.plate_text is None for event in events):
        st.caption("ANPR stage not enabled — plates are not read.")


def render_status_banner(state: CameraState | None, healthy: bool) -> None:
    """Explicit states: worker unreachable / no data yet / stale / running.

    A stale dashboard says so rather than looking live — `CameraState.is_stale`
    is the single source of that judgment.
    """
    if not healthy:
        st.error("Worker unreachable. The map still renders; live data resumes once it's back.")
        return
    if state is None:
        st.info("Worker is reachable, but this camera has not published data yet.")
        return
    if state.is_stale(get_settings().state_ttl_seconds):
        st.warning(
            f"Stale — last update {state.age_seconds():.0f}s ago. The pipeline may have stopped."
        )
        return
    if state.status == PipelineStatus.ERROR:
        st.error(f"Pipeline error: {state.error or 'unknown error'}")
        return
    st.success(f"Live — updated {state.age_seconds():.0f}s ago.")


def resolve_click(popup: object, fallback_camera_id: str) -> str:
    """Resolve a `st_folium` `last_object_clicked_popup` value to a camera id.

    `popup` is `None` on first render and otherwise whatever the map marker's
    popup text was — both are validated through the camera registry allowlist
    (`traffic_ai.cameras.get_camera`). An unknown or missing value falls back
    to `fallback_camera_id`; this never raises `KeyError`.
    """
    if not isinstance(popup, str):
        return fallback_camera_id
    camera = get_camera(popup)
    return camera.camera_id if camera is not None else fallback_camera_id
