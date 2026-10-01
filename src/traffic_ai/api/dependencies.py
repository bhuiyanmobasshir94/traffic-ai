"""Shared FastAPI dependencies: settings, the store, the camera allowlist, and history.

Each dependency reads from the running app's `request.app.state` rather than a
process-wide singleton, so tests can inject a fake store and a stub settings
object without monkeypatching module globals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, NamedTuple, Protocol

from fastapi import HTTPException, Query, Request

from traffic_ai.cameras import DEFAULT_COUNTING_DISABLED_REASON, CameraConfig, get_camera
from traffic_ai.config import Settings
from traffic_ai.domain import CrossingEvent
from traffic_ai.store import StateStore

# A window the caller did not bound ends now and starts a day earlier.
DEFAULT_HISTORY_WINDOW = timedelta(hours=24)
# Ceiling on a requested window. The aggregates are GROUP BYs over an indexed range, so
# this is a guard against an accidental "since=1970" scanning the whole table on a small
# database server, not a limit the data itself imposes.
MAX_HISTORY_WINDOW = timedelta(days=31)


def get_settings_dep(request: Request) -> Settings:
    """The `Settings` instance the app was built with."""
    return request.app.state.settings


def get_store_dep(request: Request) -> StateStore:
    """The `StateStore` instance the lifespan built, or was given."""
    return request.app.state.store


def get_camera_or_404(camera_id: str) -> CameraConfig:
    """Validate `camera_id` against the registry allowlist.

    Anything not in `traffic_ai.cameras.CAMERAS` is a 404 — never interpolated
    into a Redis key or a path. This is a security boundary; fail closed.
    """
    camera = get_camera(camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail=f"unknown camera: {camera_id}")
    return camera


def _require_counting(camera: CameraConfig) -> CameraConfig:
    """409 for a camera whose counting is switched off, carrying its reason.

    The crossing data an endpoint would serve for such a camera is not a measurement, so
    the endpoint refuses rather than answering with an empty list or zeros that read as
    "no traffic". 409, not 404: the camera exists and its live state, frame and stream are
    served; it is this one resource that is in a state that cannot answer.
    """
    if not camera.counting_enabled:
        raise HTTPException(
            status_code=409,
            detail=camera.counting_disabled_reason or DEFAULT_COUNTING_DISABLED_REASON,
        )
    return camera


def get_counting_camera_or_409(camera_id: str) -> CameraConfig:
    """`get_camera_or_404`, then 409 if the camera is not calibrated for counting.

    For the per-camera routes that serve crossings. The live state, frame and stream use
    `get_camera_or_404` alone: video and `active_tracks` are real for an uncalibrated
    camera. Unknown is answered 404 before calibration is considered.
    """
    return _require_counting(get_camera_or_404(camera_id))


# --- history ----------------------------------------------------------------


class HistoryReader(Protocol):
    """The slice of `traffic_ai.db.sink.RepositoryHistory` the history routes use.

    Structural, so tests inject a fake without a database or SQLAlchemy in sight.
    """

    async def recent(self, camera_id: str | None, limit: int) -> list[CrossingEvent]: ...

    async def counts_by_class(
        self, camera_id: str | None, since: datetime, until: datetime
    ) -> dict[str, dict[str, int]]: ...

    async def hourly_totals(
        self, camera_id: str | None, since: datetime, until: datetime
    ) -> list[tuple[datetime, int]]: ...


class HistoryWindow(NamedTuple):
    """A validated, UTC, half-open `[since, until)` interval."""

    since: datetime
    until: datetime


def get_history_camera_id(camera_id: Annotated[str | None, Query()] = None) -> str | None:
    """The optional `camera_id` filter, resolved through the registry allowlist.

    Returns the registry's own id rather than echoing the request value, so what
    reaches a query is never the caller's string. Absent means "every camera"; a
    present-but-unknown value (an empty string included) is a 404, not a silent
    widening to all cameras. A camera whose counting is switched off is a 409 with its
    reason: it records no crossings, so an empty history for it would read as measured.

    This is the first dependency on every history route, so camera errors (404, then 409)
    are answered before a window error (422) and before availability (503).
    """
    if camera_id is None:
        return None
    return get_counting_camera_or_409(camera_id).camera_id


def get_history_window(
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
) -> HistoryWindow:
    """Resolve `since`/`until` into a validated UTC window, or raise 422.

    Naive datetimes are REJECTED rather than assumed to be UTC: a client that sends
    local time without an offset would otherwise get a silently shifted window and
    plausible-looking, wrong numbers. Offsets other than UTC are accepted and
    normalised, so the echoed bounds are always UTC.
    """
    for name, value in (("since", since), ("until", until)):
        if value is not None and value.utcoffset() is None:
            raise HTTPException(
                status_code=422,
                detail=f"{name} must include a UTC offset (e.g. 2026-03-01T10:00:00Z)",
            )

    # Converting to UTC can leave the representable range (`9999-12-31T23:59:59-05:00` is
    # year 10000 in UTC), and so can stepping back a default window from a very early
    # `until`. Both raise `OverflowError`, which would surface as a 500 for what is a
    # malformed request.
    try:
        end = until.astimezone(UTC) if until is not None else datetime.now(UTC)
        start = since.astimezone(UTC) if since is not None else end - DEFAULT_HISTORY_WINDOW
    except OverflowError:
        raise HTTPException(
            status_code=422,
            detail="since and until must be representable as UTC datetimes",
        ) from None

    if start >= end:
        raise HTTPException(status_code=422, detail="since must be earlier than until")
    if end - start > MAX_HISTORY_WINDOW:
        raise HTTPException(
            status_code=422,
            detail=f"window may not exceed {MAX_HISTORY_WINDOW.days} days",
        )
    return HistoryWindow(since=start, until=end)


class _UnavailableHistory:
    """Stands in for the history source when there is no database to read from.

    Every query raises a 503 carrying the reason. This is a dependency's return
    value rather than the dependency raising, deliberately: FastAPI runs every
    dependency before it validates the endpoint's own parameters, so a dependency
    that raised would answer an invalid request (`limit=0`, an unparseable
    `since`) with a 503 instead of the 422 it deserves. Raising on first use
    keeps validation errors ahead of availability errors.
    """

    def __init__(self, reason: str) -> None:
        self._detail = f"history is unavailable: {reason}"

    def _unavailable(self) -> HTTPException:
        return HTTPException(status_code=503, detail=self._detail)

    async def recent(self, camera_id: str | None, limit: int) -> list[CrossingEvent]:
        raise self._unavailable()

    async def counts_by_class(
        self, camera_id: str | None, since: datetime, until: datetime
    ) -> dict[str, dict[str, int]]:
        raise self._unavailable()

    async def hourly_totals(
        self, camera_id: str | None, since: datetime, until: datetime
    ) -> list[tuple[datetime, int]]:
        raise self._unavailable()


def get_history_reader(request: Request) -> HistoryReader:
    """The history source the lifespan built, or one that answers every query with 503.

    Nothing is fabricated in the unavailable case: no empty list, no zero counts.
    """
    reader = getattr(request.app.state, "history", None)
    if reader is not None:
        return reader
    settings: Settings = request.app.state.settings
    if not settings.persistence_enabled:
        return _UnavailableHistory("persistence is disabled")
    return _UnavailableHistory("the database was not initialised")
