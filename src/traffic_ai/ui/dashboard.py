"""One page layout, rendered for both entrypoints with a different camera and
heading. `Toll_Booth.py` and `pages/Traffic_Analysis.py` are thin callers of
`render_dashboard` — see `CLAUDE.md` on why a fix here must never become a
second copy.
"""

from __future__ import annotations

import streamlit as st

from traffic_ai.config import get_settings
from traffic_ai.domain import CrossingEvent
from traffic_ai.ui.client import (
    ApiUnauthorized,
    CountingNotCalibrated,
    WorkerUnavailable,
    get_worker_client,
)
from traffic_ai.ui.components import (
    counting_disabled_reason,
    render_counting_disabled,
    render_demo_footage_notice,
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
    render_demo_footage_notice()

    client = get_worker_client()
    settings = get_settings()
    healthy = client.healthy()

    try:
        states = client.states() if healthy else {}
    except ApiUnauthorized:
        # Not "unreachable": the live region below says what is wrong. The map still
        # renders, with no per-camera state on it.
        states = {}
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
    script thread: no blocking sleeps, no driving a generator.

    A camera that is not calibrated for counting gets its reason and live track count
    in place of the stats and events: none of those numbers would be a measurement."""
    client = get_worker_client()
    healthy = client.healthy()
    state = None
    events: list[CrossingEvent] = []
    unauthorized: str | None = None
    worker_not_calibrated: str | None = None
    if healthy:
        try:
            state = client.state(camera_id)
            # An uncalibrated camera records no crossings, and an empty table would read as
            # a measured "none", so its events are neither fetched nor shown.
            if counting_disabled_reason(camera_id) is None:
                events = client.events(camera_id, limit=25)
        except ApiUnauthorized as exc:
            # `ApiUnauthorized` is a `WorkerUnavailable`, so it must be caught first. The
            # liveness probe is auth-exempt, which is why `healthy` is still True here.
            unauthorized = str(exc)
        except CountingNotCalibrated as exc:
            # The worker refused crossings (409) for a camera this UI's registry thinks
            # counts. The worker is up, so this is not "unreachable"; and it is the stricter
            # of the two opinions, so it wins below.
            worker_not_calibrated = str(exc)
        except WorkerUnavailable:
            healthy = False

    render_status_banner(state, healthy, unauthorized=unauthorized)
    reason = counting_disabled_reason(camera_id, state) or worker_not_calibrated
    if reason is not None:
        render_counting_disabled(reason, state)
        return
    render_stats(state)
    render_events(events)
