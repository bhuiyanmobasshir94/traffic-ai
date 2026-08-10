"""One page layout, rendered for both entrypoints with a different camera and
heading. `Toll_Booth.py` and `pages/Traffic_Analysis.py` are thin callers of
`render_dashboard` — see `CLAUDE.md` on why a fix here must never become a
second copy.
"""

from __future__ import annotations

import streamlit as st

from traffic_ai.config import get_settings
from traffic_ai.domain import CrossingEvent
from traffic_ai.ui.client import WorkerUnavailable, get_worker_client
from traffic_ai.ui.components import (
    render_events,
    render_live_video,
    render_map,
    render_sidebar,
    render_stats,
    render_status_banner,
    resolve_click,
)


def render_dashboard(*, page_key: str, title: str, default_camera_id: str) -> None:
    """Render one page: video, map, and a live-refreshing stats/events region.

    `page_key` namespaces every session-state key so the toll-booth and
    traffic-analysis pages never clobber each other's selected camera — the
    problem the original `VIDEO_URL` / `T_VIDEO_URL` split was working around.
    Initialised here, before any read, so the video is never blank on first
    render (the old code set it inside a branch that didn't render).
    """
    selected_key = f"{page_key}:selected_camera_id"
    if selected_key not in st.session_state:
        st.session_state[selected_key] = default_camera_id

    render_sidebar()
    st.title(title)

    client = get_worker_client()
    settings = get_settings()
    healthy = client.healthy()

    try:
        states = client.states() if healthy else {}
    except WorkerUnavailable:
        states = {}
        healthy = False

    video_col, map_col = st.columns([3, 2])

    with video_col:
        render_live_video(st.session_state[selected_key], settings.api_public_url)

    with map_col:
        map_data = render_map(states, key=f"{page_key}-map")
        popup = (map_data or {}).get("last_object_clicked_popup")
        st.session_state[selected_key] = resolve_click(popup, st.session_state[selected_key])

    _render_live_region(camera_id=st.session_state[selected_key])


@st.fragment(run_every="2s")
def _render_live_region(*, camera_id: str) -> None:
    """Stats, events, and the status banner — refreshed on their own every 2s
    without rerunning the rest of the page, and without ever blocking the
    script thread: no blocking sleeps, no driving a generator."""
    client = get_worker_client()
    healthy = client.healthy()
    state = None
    events: list[CrossingEvent] = []
    if healthy:
        try:
            state = client.state(camera_id)
            events = client.events(camera_id, limit=25)
        except WorkerUnavailable:
            healthy = False

    render_status_banner(state, healthy)
    render_stats(state)
    render_events(events)
