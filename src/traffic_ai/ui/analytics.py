"""Historical analytics: what the worker persisted to Postgres, over a window.

Everything on the page is read from the API's `/history/*` endpoints through
`WorkerClient`; the UI never touches the database. Four outcomes are kept
distinct on purpose, because each means something different to the person
looking at the screen:

- `OK`           — there is history to show.
- `EMPTY`        — history works and the window genuinely holds no crossings.
                   A real, measured zero, not a failure.
- `UNAVAILABLE`  — the API answered 503: persistence is off or the database is
                   down. We know nothing about the window.
- `UNAUTHORIZED` / `ERROR` — the UI could not ask (bad token) or could not
                   reach the worker. Also "we know nothing".

Nothing here fills a gap with a made-up value: a class missing from the counts
payload is a zero crossings of that class, an hour missing from the trend is
simply absent from the line, and an unread plate is shown as a dash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import streamlit as st

from traffic_ai.cameras import CAMERAS, get_camera
from traffic_ai.domain import VEHICLE_CLASSES, CrossingEvent, Direction
from traffic_ai.ui.client import (
    ApiUnauthorized,
    HistoryCounts,
    HistoryHourly,
    HistoryUnavailable,
    WorkerClient,
    WorkerUnavailable,
    get_worker_client,
)
from traffic_ai.ui.components import build_event_rows, render_sidebar

ALL_CAMERAS = "All cameras"

WINDOWS: dict[str, timedelta] = {
    "Last hour": timedelta(hours=1),
    "Last 24 hours": timedelta(hours=24),
    "Last 7 days": timedelta(days=7),
}
DEFAULT_WINDOW = "Last 24 hours"

# Rows asked of `/history/events`. That endpoint takes a limit, not a time range,
# so the table is filtered to the window afterwards and may hold fewer than this.
EVENTS_LIMIT = 50

_EVENT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class HistoryStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    UNAUTHORIZED = "unauthorized"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True)
class HistoryView:
    """Everything the page needs to render one window, or why it has nothing."""

    status: HistoryStatus
    message: str = ""
    counts: HistoryCounts | None = None
    hourly: HistoryHourly | None = None
    events: tuple[CrossingEvent, ...] = ()


# --- pure helpers (no Streamlit; unit-tested directly) -------------------------


def camera_options() -> list[str]:
    """Selector choices: the sentinel first, then every registered camera id."""
    return [ALL_CAMERAS, *(camera.camera_id for camera in CAMERAS)]


def camera_label(choice: str) -> str:
    if choice == ALL_CAMERAS:
        return ALL_CAMERAS
    camera = get_camera(choice)
    return camera.name if camera is not None else choice


def resolve_camera(choice: object) -> str | None:
    """Selector value -> `camera_id` for the API, or `None` for all cameras.

    Resolved through the camera registry allowlist (`traffic_ai.cameras.get_camera`),
    never passed on as-is: an unrecognised value falls back to "all cameras"
    instead of reaching a query string.
    """
    if not isinstance(choice, str) or choice == ALL_CAMERAS:
        return None
    camera = get_camera(choice)
    return camera.camera_id if camera is not None else None


def window_bounds(label: object, *, now: datetime) -> tuple[datetime, datetime]:
    """`(since, until)` for a window label; an unknown label means the default."""
    delta = WINDOWS.get(label) if isinstance(label, str) else None
    return now - (delta or WINDOWS[DEFAULT_WINDOW]), now


def counts_frame(counts: HistoryCounts) -> dict[str, list[object]]:
    """Direction x class totals as a column-oriented table, one row per class.

    Known vehicle classes come first in their canonical order; any class the
    API reports beyond them is appended so the table always accounts for every
    crossing in the payload.
    """
    classes = list(VEHICLE_CLASSES)
    seen = {name for per_direction in counts.counts.values() for name in per_direction}
    classes.extend(sorted(seen - set(classes)))

    frame: dict[str, list[object]] = {"class": list(classes)}
    for direction in Direction:
        per_class = counts.counts.get(direction, {})
        frame[direction.value] = [per_class.get(name, 0) for name in classes]
    return frame


def direction_totals(counts: HistoryCounts) -> dict[Direction, int]:
    return {direction: sum(counts.counts.get(direction, {}).values()) for direction in Direction}


def hourly_frame(hourly: HistoryHourly) -> dict[str, list[object]]:
    """The API's buckets, in time order, exactly as returned — hours the API
    did not report are not filled in with zeros."""
    buckets = sorted(hourly.buckets, key=lambda bucket: bucket.hour)
    return {
        "hour": [bucket.hour for bucket in buckets],
        "crossings": [bucket.total for bucket in buckets],
    }


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def fetch_history(
    client: WorkerClient,
    *,
    camera_id: str | None,
    since: datetime,
    until: datetime,
    events_limit: int = EVENTS_LIMIT,
) -> HistoryView:
    """Load one window and classify the outcome. Never raises on API trouble:
    every failure becomes a status the page renders."""
    try:
        counts = client.history_counts(since=since, until=until, camera_id=camera_id)
        hourly = client.history_hourly(since=since, until=until, camera_id=camera_id)
        events = client.history_events(camera_id, limit=events_limit)
    except ApiUnauthorized as exc:
        return HistoryView(HistoryStatus.UNAUTHORIZED, message=str(exc))
    except HistoryUnavailable as exc:
        return HistoryView(HistoryStatus.UNAVAILABLE, message=str(exc))
    except WorkerUnavailable as exc:
        return HistoryView(HistoryStatus.ERROR, message=str(exc))

    windowed = tuple(event for event in events if _as_utc(event.crossed_at) >= since)
    status = HistoryStatus.OK if counts.total > 0 or windowed else HistoryStatus.EMPTY
    return HistoryView(status, counts=counts, hourly=hourly, events=windowed)


# --- rendering -----------------------------------------------------------------


def render_history(view: HistoryView) -> None:
    """One branch per status. No branch renders an empty container."""
    if view.status is HistoryStatus.UNAUTHORIZED:
        st.error(view.message)
        return
    if view.status is HistoryStatus.UNAVAILABLE:
        st.warning(view.message)
        st.caption("Live counts on the dashboard pages are not affected.")
        return
    if view.status is HistoryStatus.ERROR:
        st.error(f"Could not load history. {view.message}")
        return
    if view.status is HistoryStatus.EMPTY or view.counts is None or view.hourly is None:
        st.info("No crossings recorded in this window.")
        return

    totals = direction_totals(view.counts)
    metric_cols = st.columns(len(Direction) + 1)
    metric_cols[0].metric("Total crossings", view.counts.total)
    for col, direction in zip(metric_cols[1:], Direction, strict=True):
        col.metric(direction.value.title(), totals[direction])

    st.subheader("Crossings by direction and class")
    frame = counts_frame(view.counts)
    table_col, chart_col = st.columns(2)
    table_col.dataframe(frame, hide_index=True, width="stretch")
    chart_col.bar_chart(frame, x="class", y=[direction.value for direction in Direction])

    st.subheader("Hourly trend")
    if view.hourly.buckets:
        st.line_chart(hourly_frame(view.hourly), x="hour", y="crossings")
    else:
        st.caption("No hourly breakdown was returned for this window.")

    st.subheader("Most recent crossings in this window")
    if view.events:
        rows = build_event_rows(view.events, time_format=_EVENT_TIME_FORMAT)
        st.dataframe(rows, hide_index=True, width="stretch")
        st.caption("Times are UTC. A dash in the plate column means no plate was read.")
    else:
        st.caption("No individual crossings were returned for this window.")


def render_analytics(*, page_key: str, title: str) -> None:
    """The Analytics page. `page_key` namespaces session state, as in
    `render_dashboard`, and defaults are set before any widget reads them."""
    camera_key = f"{page_key}:camera"
    window_key = f"{page_key}:window"
    if camera_key not in st.session_state:
        st.session_state[camera_key] = ALL_CAMERAS
    if window_key not in st.session_state:
        st.session_state[window_key] = DEFAULT_WINDOW

    render_sidebar()
    st.title(title)

    camera_col, window_col = st.columns(2)
    camera_choice = camera_col.selectbox(
        "Camera", options=camera_options(), format_func=camera_label, key=camera_key
    )
    window_choice = window_col.selectbox("Window", options=list(WINDOWS), key=window_key)

    since, until = window_bounds(window_choice, now=datetime.now(UTC))
    st.caption(f"{since:%Y-%m-%d %H:%M} to {until:%Y-%m-%d %H:%M} UTC")

    view = fetch_history(
        get_worker_client(), camera_id=resolve_camera(camera_choice), since=since, until=until
    )
    render_history(view)
