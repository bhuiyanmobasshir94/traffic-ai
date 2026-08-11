"""The `/api` route table.

Every path here is served to the browser through Traefik's `PathPrefix(/api)`
straight through — no strip-prefix middleware — so the paths declared on this
router are exactly the public paths.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from traffic_ai import __version__
from traffic_ai.api.dependencies import get_camera_or_404, get_settings_dep, get_store_dep
from traffic_ai.cameras import CAMERAS, CameraConfig
from traffic_ai.config import Settings
from traffic_ai.domain import (
    CameraState,
    CameraSummary,
    CrossingEvent,
    HealthResponse,
    PipelineStatus,
    ReadinessResponse,
)
from traffic_ai.logging import get_logger
from traffic_ai.store import StateStore

logger = get_logger(__name__)

router = APIRouter(prefix="/api")

# `Annotated[..., Depends(...)]` rather than `= Depends(...)`: the latter is a
# function call in a default argument, which is exactly what ruff's B008
# flags — and correctly so in general, just not for FastAPI's own dependency
# markers, which this codebase has no `extend-immutable-calls` config to
# exempt (`pyproject.toml` is outside this task's read-write set).
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
StoreDep = Annotated[StateStore, Depends(get_store_dep)]
CameraDep = Annotated[CameraConfig, Depends(get_camera_or_404)]


@router.get("/healthz", response_model=HealthResponse)
async def healthz(settings: SettingsDep) -> HealthResponse:
    """Liveness. Always 200 while the process is up — never touches Redis."""
    return HealthResponse(status="ok", version=__version__, environment=settings.environment)


@router.get("/readyz")
async def readyz(request: Request, store: StoreDep) -> JSONResponse:
    """Readiness. Ready only when Redis pings and at least one pipeline is
    actually RUNNING. 503 otherwise, so a load balancer stops sending traffic
    without restarting the container (that's what liveness is for).

    Only RUNNING counts. STARTING has nothing to serve yet, STALLED cannot
    produce a fresh frame, and STOPPED is a container draining on shutdown —
    treating any of them as ready would keep traffic arriving at an instance
    that cannot answer it.
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

    body = ReadinessResponse(
        ready=ready,
        redis=redis_ok,
        cameras_running=cameras_running,
        cameras_total=cameras_total,
        detail=detail,
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
    store: StateStore, camera_id: str, *, fps: float, ttl_seconds: float
) -> AsyncIterator[bytes]:
    """Poll the store for new frames and yield multipart/x-mixed-replace parts.

    Skips re-sending an unchanged frame so a stalled pipeline does not
    saturate the connection, and ends the stream if no *new* frame has shown
    up within `ttl_seconds` rather than holding the connection open forever.
    """
    interval = 1.0 / fps
    last_frame: bytes | None = None
    last_new_frame_at = time.monotonic()
    try:
        while True:
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
    camera: CameraDep, store: StoreDep, settings: SettingsDep
) -> StreamingResponse:
    frames = _mjpeg_frames(
        store,
        camera.camera_id,
        fps=settings.target_fps,
        ttl_seconds=settings.state_ttl_seconds,
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
