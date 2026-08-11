"""FastAPI application: process lifespan, route wiring, and request-id logging.

This process doubles as the worker host: the lifespan starts each camera
pipeline as a background task and the routes read the state those pipelines
publish to Redis (`traffic_ai.store.StateStore`). The pipeline implementation
itself lives in `traffic_ai.worker`, built separately; this module never
imports it at module scope — only lazily, inside `default_pipeline_factory`,
so `traffic_ai.api.app` stays importable with the worker package absent.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Protocol

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import Response

from traffic_ai.api.routes import router
from traffic_ai.config import Settings, get_settings
from traffic_ai.domain import CameraState
from traffic_ai.logging import configure_logging, get_logger
from traffic_ai.store import StateStore

logger = get_logger(__name__)

# Ceiling on how long shutdown waits for pipeline tasks to notice
# `request_stop()` and return before we cancel them outright.
_SHUTDOWN_TIMEOUT_SECONDS = 10.0


class CameraPipeline(Protocol):
    """The worker's pipeline interface, declared rather than imported.

    Keeping this a structural Protocol — instead of `from traffic_ai.worker...
    import CameraPipeline` — is what lets this module stay import-safe while
    the worker package is built in parallel.
    """

    @property
    def camera_id(self) -> str: ...

    @property
    def state(self) -> CameraState: ...

    async def run(self) -> None: ...

    def request_stop(self) -> None: ...


PipelineFactory = Callable[[Settings, StateStore], list[CameraPipeline]]


def default_pipeline_factory(settings: Settings, store: StateStore) -> list[CameraPipeline]:
    """Build the real pipelines. Imports `traffic_ai.worker` lazily so importing
    this module never requires the worker package to exist."""
    from traffic_ai.worker.pipeline import build_pipelines

    return build_pipelines(settings, store)


async def _run_pipeline(pipeline: CameraPipeline) -> None:
    """Run one pipeline task to completion. A crash is logged, not swallowed —
    `pipeline.run()` is documented never to raise, but a background task that
    dies silently is worse than one that logs and stops."""
    try:
        await pipeline.run()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("pipeline.crashed", camera_id=pipeline.camera_id)


async def _request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Bind a per-request id into structlog contextvars and echo it back.
    Accepts an inbound `X-Request-ID` so a caller's trace id survives."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id)
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.unbind_contextvars("request_id")
    response.headers["X-Request-ID"] = request_id
    return response


def create_app(
    *,
    settings: Settings | None = None,
    store: StateStore | None = None,
    pipeline_factory: PipelineFactory = default_pipeline_factory,
) -> FastAPI:
    """Build the ASGI app.

    `store` and `pipeline_factory` are injectable so tests never need a real
    Redis server or a real inference pipeline. When `store` is given, this app
    does not own its lifecycle and will not close it on shutdown — the caller
    (a fixture, typically) does.
    """
    resolved_settings = settings or get_settings()
    injected_store = store

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(
            level=resolved_settings.log_level,
            fmt=resolved_settings.log_format,
            service="traffic-ai-worker",
        )

        owns_store = injected_store is None
        state_store = injected_store or StateStore.from_url(
            resolved_settings.redis_url,
            ttl_seconds=resolved_settings.state_ttl_seconds,
            event_history=resolved_settings.event_history,
        )

        pipelines = pipeline_factory(resolved_settings, state_store)
        tasks = [asyncio.create_task(_run_pipeline(p)) for p in pipelines]

        app.state.settings = resolved_settings
        app.state.store = state_store
        app.state.pipelines = pipelines
        app.state.pipeline_tasks = tasks

        logger.info("api.startup", camera_count=len(pipelines))
        try:
            yield
        finally:
            for pipeline in pipelines:
                pipeline.request_stop()
            if tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=_SHUTDOWN_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    logger.warning("api.shutdown_timeout", pending_tasks=len(tasks))
                    for task in tasks:
                        task.cancel()
            if owns_store:
                await state_store.close()
            logger.info("api.shutdown")

    app = FastAPI(lifespan=lifespan)
    app.middleware("http")(_request_id_middleware)
    app.include_router(router)
    return app
