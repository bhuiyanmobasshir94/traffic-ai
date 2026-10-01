"""The `/api` route table.

Every path here is served to the browser through Traefik's `PathPrefix(/api)`
straight through — no strip-prefix middleware — so the paths declared on this
router are exactly the public paths.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from traffic_ai import __version__
from traffic_ai.api.dependencies import (
    HistoryReader,
    HistoryWindow,
    get_camera_or_404,
    get_history_camera_id,
    get_history_reader,
    get_history_window,
    get_settings_dep,
    get_store_dep,
)
from traffic_ai.cameras import CAMERAS, CameraConfig
from traffic_ai.config import Settings
from traffic_ai.db.errors import describe_db_error
from traffic_ai.domain import (
    CameraState,
    CameraSummary,
    CrossingEvent,
    Direction,
    HealthResponse,
    HistoryCounts,
    HourlyBucket,
    HourlyTotals,
    PipelineStatus,
    ReadinessResponse,
)
from traffic_ai.logging import get_logger
from traffic_ai.store import StateStore

logger = get_logger(__name__)

router = APIRouter(prefix="/api")

# How long readiness waits on Postgres before reporting it down. Bounded because a
# probe that hangs on a black-holed database host would time out the orchestrator's
# own readiness check and take the instance out of rotation, which is exactly what
# a database outage is not allowed to do.
_DATABASE_PROBE_TIMEOUT_SECONDS = 2.0

# Ceiling on one history query, connect included. Kept under the UI's default
# `api_timeout_seconds` (5s) so the caller gets this route's 503 and its reason
# rather than giving up on a request that is still holding a pooled connection.
_HISTORY_QUERY_TIMEOUT_SECONDS = 4.0

# `Annotated[..., Depends(...)]` rather than `= Depends(...)`: the latter is a
# function call in a default argument, which is exactly what ruff's B008
# flags — and correctly so in general, just not for FastAPI's own dependency
# markers, which this codebase has no `extend-immutable-calls` config to
# exempt (`pyproject.toml` is outside this task's read-write set).
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
StoreDep = Annotated[StateStore, Depends(get_store_dep)]
CameraDep = Annotated[CameraConfig, Depends(get_camera_or_404)]
HistoryCameraDep = Annotated[str | None, Depends(get_history_camera_id)]
HistoryWindowDep = Annotated[HistoryWindow, Depends(get_history_window)]
HistoryDep = Annotated[HistoryReader, Depends(get_history_reader)]


@router.get("/healthz", response_model=HealthResponse)
async def healthz(settings: SettingsDep) -> HealthResponse:
    """Liveness. Always 200 while the process is up — never touches Redis."""
    return HealthResponse(status="ok", version=__version__, environment=settings.environment)


async def _probe_database(request: Request, settings: Settings) -> bool | None:
    """`None` when persistence is off, otherwise whether the database is usable: it
    answered AND has the `crossing_events` table (see `Database.ping`).

    Never raises and never blocks past `_DATABASE_PROBE_TIMEOUT_SECONDS`.
    """
    if not settings.persistence_enabled:
        return None
    database = getattr(request.app.state, "database", None)
    if database is None:
        # Persistence is on but setup failed at startup: history is down.
        return False
    try:
        return await asyncio.wait_for(database.ping(), timeout=_DATABASE_PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        return False


@router.get("/readyz")
async def readyz(request: Request, store: StoreDep, settings: SettingsDep) -> JSONResponse:
    """Readiness. Ready only when Redis pings and at least one pipeline is
    actually RUNNING. 503 otherwise, so a load balancer stops sending traffic
    without restarting the container (that's what liveness is for).

    Only RUNNING counts. STARTING has nothing to serve yet, STALLED cannot
    produce a fresh frame, and STOPPED is a container draining on shutdown —
    treating any of them as ready would keep traffic arriving at an instance
    that cannot answer it.

    The database is reported (`database`, which is False for a reachable server
    whose schema has not been migrated) but never gates readiness: the live
    path does not depend on it, so an instance with Postgres down still serves
    counts, frames and the stream, and should keep receiving traffic for them.
    `history_events_lost` is reported on the same terms.
    """
    redis_ok = await store.ping()
    pipelines = getattr(request.app.state, "pipelines", [])
    cameras_total = len(pipelines)
    cameras_running = sum(1 for p in pipelines if p.state.status == PipelineStatus.RUNNING)
    ready = redis_ok and cameras_running > 0

    detail: str | None = None
    if not redis_ok:
        detail = "redis unreachable"
    elif cameras_running == 0:
        detail = "no camera pipeline running"

    writer = getattr(request.app.state, "history_writer", None)
    body = ReadinessResponse(
        ready=ready,
        redis=redis_ok,
        cameras_running=cameras_running,
        cameras_total=cameras_total,
        detail=detail,
        database=await _probe_database(request, settings),
        history_events_lost=writer.lost_count if writer is not None else None,
    )
    return JSONResponse(status_code=200 if ready else 503, content=body.model_dump())


@router.get("/cameras", response_model=list[CameraSummary])
async def list_cameras() -> list[CameraSummary]:
    """Static camera registry — does not touch the store."""
    return [
        CameraSummary(
            camera_id=c.camera_id,
            name=c.name,
            latitude=c.latitude,
            longitude=c.longitude,
            description=c.description,
        )
        for c in CAMERAS
    ]


@router.get("/cameras/{camera_id}/state", response_model=CameraState)
async def camera_state(camera: CameraDep, store: StoreDep) -> CameraState:
    state = await store.read_state(camera.camera_id)
    if state is None:
        raise HTTPException(status_code=503, detail="no state published yet")
    return state


@router.get("/cameras/{camera_id}/frame.jpg")
async def camera_frame(camera: CameraDep, store: StoreDep) -> Response:
    frame = await store.read_frame(camera.camera_id)
    if frame is None:
        raise HTTPException(status_code=503, detail="no frame published yet")
    return Response(content=frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def _mjpeg_frames(
    store: StateStore,
    camera_id: str,
    *,
    fps: float,
    ttl_seconds: float,
    stop: asyncio.Event | None = None,
) -> AsyncIterator[bytes]:
    """Poll the store for new frames and yield multipart/x-mixed-replace parts.

    Skips re-sending an unchanged frame so a stalled pipeline does not
    saturate the connection, and ends the stream if no *new* frame has shown
    up within `ttl_seconds` rather than holding the connection open forever.

    `stop` is the app's shutdown event. A viewer on a healthy pipeline never
    hits the stall exit, so without it an open stream would keep the server from
    finishing a graceful shutdown for as long as the viewer keeps the tab open.
    Checked once per loop, so the stream ends within one frame interval of it.
    """
    interval = 1.0 / fps
    last_frame: bytes | None = None
    last_new_frame_at = time.monotonic()
    try:
        while stop is None or not stop.is_set():
            frame = await store.read_frame(camera_id)
            now = time.monotonic()
            if frame is not None and frame != last_frame:
                last_frame = frame
                last_new_frame_at = now
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
                )
            if now - last_new_frame_at > ttl_seconds:
                logger.debug("stream.stalled", camera_id=camera_id)
                return
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.debug("stream.client_disconnected", camera_id=camera_id)
        return


@router.get("/cameras/{camera_id}/stream.mjpg")
async def camera_stream(
    request: Request, camera: CameraDep, store: StoreDep, settings: SettingsDep
) -> StreamingResponse:
    frames = _mjpeg_frames(
        store,
        camera.camera_id,
        fps=settings.target_fps,
        ttl_seconds=settings.state_ttl_seconds,
        stop=getattr(request.app.state, "shutdown_event", None),
    )
    return StreamingResponse(frames, media_type="multipart/x-mixed-replace; boundary=frame")


@router.get("/cameras/{camera_id}/events", response_model=list[CrossingEvent])
async def camera_events(
    camera: CameraDep,
    store: StoreDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[CrossingEvent]:
    return await store.read_events(camera.camera_id, limit)


@router.get("/events", response_model=list[CrossingEvent])
async def all_events(
    store: StoreDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[CrossingEvent]:
    """Merged, newest-first feed across every registered camera."""
    camera_ids = [c.camera_id for c in CAMERAS]
    return await store.read_events_multi(camera_ids, limit)


# --- history (Postgres) -----------------------------------------------------
# Served from the database, not Redis: these survive a worker restart and a state
# TTL, which the live `/events` and `/cameras/{id}/state` routes do not. Every one is
# a 503 — never an empty list or a zero — when there is no database to answer.


async def _history_query[T](query: Awaitable[T]) -> T:
    """Await one history query, turning any database failure into a 503.

    The broad catch is deliberate and confined to the query: a driver, pool, or
    network error is "history is down", which the caller can act on, not a 500 that
    reads as a bug in this service. The cause is logged; it is not echoed to the
    client. An `HTTPException` (the 503 from an unavailable source) passes through.
    """
    try:
        async with asyncio.timeout(_HISTORY_QUERY_TIMEOUT_SECONDS):
            return await query
    except HTTPException:
        raise
    except TimeoutError:
        logger.warning("history.query_timeout", timeout_s=_HISTORY_QUERY_TIMEOUT_SECONDS)
        raise HTTPException(
            status_code=503, detail="history is unavailable: the database did not respond in time"
        ) from None
    except Exception as exc:
        # Never `str(exc)`: for a SQLAlchemy error that is the statement, the bound
        # parameters and the driver's message, which can quote row values.
        logger.warning("history.query_failed", **describe_db_error(exc))
        raise HTTPException(
            status_code=503, detail="history is unavailable: the database query failed"
        ) from None


@router.get("/history/events", response_model=list[CrossingEvent])
async def history_events(
    camera_id: HistoryCameraDep,
    reader: HistoryDep,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[CrossingEvent]:
    """Newest-first crossings from the database. Omit `camera_id` to merge cameras."""
    return await _history_query(reader.recent(camera_id, limit))


@router.get("/history/counts", response_model=HistoryCounts)
async def history_counts(
    camera_id: HistoryCameraDep, window: HistoryWindowDep, reader: HistoryDep
) -> HistoryCounts:
    """Crossings in `[since, until)` by direction and vehicle class."""
    raw = await _history_query(reader.counts_by_class(camera_id, window.since, window.until))
    counts = {direction: dict(raw.get(direction.value, {})) for direction in Direction}
    return HistoryCounts(
        camera_id=camera_id,
        since=window.since,
        until=window.until,
        counts=counts,
        total=sum(n for per_class in counts.values() for n in per_class.values()),
    )


@router.get("/history/hourly", response_model=HourlyTotals)
async def history_hourly(
    camera_id: HistoryCameraDep, window: HistoryWindowDep, reader: HistoryDep
) -> HourlyTotals:
    """Crossings per UTC hour in `[since, until)`, oldest first; empty hours are absent."""
    rows = await _history_query(reader.hourly_totals(camera_id, window.since, window.until))
    return HourlyTotals(
        camera_id=camera_id,
        since=window.since,
        until=window.until,
        buckets=[HourlyBucket(hour=hour, total=total) for hour, total in rows],
    )
