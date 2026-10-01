"""FastAPI application: process lifespan, route wiring, request-id logging, and
the edge-hardening middleware stack (metrics, security headers, rate limit, auth).

This process doubles as the worker host: the lifespan starts each camera
pipeline as a background task and the routes read the state those pipelines
publish to Redis (`traffic_ai.store.StateStore`). The pipeline implementation
itself lives in `traffic_ai.worker`, built separately; this module never
imports it at module scope — only lazily, inside `default_pipeline_factory`,
so `traffic_ai.api.app` stays importable with the worker package absent.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Protocol

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import Response

from traffic_ai import metrics
from traffic_ai.api.middleware import AuthMiddleware, RateLimitMiddleware, SecurityHeadersMiddleware
from traffic_ai.api.routes import router
from traffic_ai.config import Settings, get_settings
from traffic_ai.db.session import Database
from traffic_ai.db.sink import RepositoryHistory, RepositorySink
from traffic_ai.db.writer import CrossingWriter
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


# A factory takes `(settings, store)` and MAY also declare a `writer` keyword to receive the
# history writer. Factories predating persistence declare only the first two, and are
# still called that way — see `_build_pipelines`.
PipelineFactory = Callable[..., list[CameraPipeline]]


def default_pipeline_factory(
    settings: Settings, store: StateStore, writer: CrossingWriter | None = None
) -> list[CameraPipeline]:
    """Build the real pipelines. Imports `traffic_ai.worker` lazily so importing
    this module never requires the worker package to exist."""
    from traffic_ai.worker.pipeline import build_pipelines

    return build_pipelines(settings, store, writer)


def _accepts_writer(factory: PipelineFactory) -> bool:
    try:
        parameters = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "writer" or p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters)


def _build_pipelines(
    factory: PipelineFactory,
    settings: Settings,
    store: StateStore,
    writer: CrossingWriter | None,
) -> list[CameraPipeline]:
    """Offer `writer` only to a factory that declares it.

    Handing an unexpected keyword to a two-argument factory would be a `TypeError` at
    startup, so persistence is opt-in on the factory's side rather than a signature
    change every existing factory must absorb.
    """
    if writer is not None and _accepts_writer(factory):
        return factory(settings, store, writer=writer)
    return factory(settings, store)


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


async def _run_writer(writer: CrossingWriter) -> None:
    """Run the history writer to completion. `CrossingWriter.run()` is documented never
    to raise, but like `_run_pipeline` a background task must not die silently."""
    try:
        await writer.run()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("db.writer_crashed")


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


# Label for a request that matched no route (a 404, or a request an outer
# layer rejected before routing ran). A fixed string, never the raw path: see
# `_route_label`.
_UNMATCHED_ROUTE_LABEL = "unmatched"


def _route_label(request: Request) -> str:
    """The Prometheus `path` label: the matched route TEMPLATE, never the URL.

    `/api/cameras/{camera_id}/frame.jpg` is one label value however many
    camera ids are requested; the raw path would be a new value per id, and a
    404 on an attacker-chosen path would mint one per probe. An unbounded label
    set is the classic way to exhaust a Prometheus server's memory, so anything
    that did not match a route collapses to one fixed label instead.

    Starlette's router writes `scope["route"]` into the scope dict in place
    when it matches, and this request shares that dict with the inner app, so
    the value is already there by the time `call_next` returns.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else _UNMATCHED_ROUTE_LABEL


async def _metrics_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Count and time every request. Only installed when metrics are enabled.

    Observability must never break the request it observes, so the recording
    is kept to a counter increment and a histogram observation, both of which
    are in-memory and non-raising.
    """
    start = time.perf_counter()
    status = "500"
    try:
        response = await call_next(request)
        status = str(response.status_code)
        return response
    finally:
        # An exception that escapes `call_next` is about to become a 500 in
        # Starlette's outermost error handler, so it is counted as one.
        path = _route_label(request)
        metrics.http_requests_total.labels(request.method, path, status).inc()
        metrics.http_request_duration_seconds.labels(request.method, path).observe(
            time.perf_counter() - start
        )


async def _metrics_endpoint() -> Response:
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)


def create_app(
    *,
    settings: Settings | None = None,
    store: StateStore | None = None,
    pipeline_factory: PipelineFactory = default_pipeline_factory,
    database: Database | None = None,
    writer: CrossingWriter | None = None,
) -> FastAPI:
    """Build the ASGI app.

    `store` and `pipeline_factory` are injectable so tests never need a real
    Redis server or a real inference pipeline. When `store` is given, this app
    does not own its lifecycle and will not close it on shutdown — the caller
    (a fixture, typically) does. `database` follows the same rule.

    `database` and `writer` are likewise injectable, and only consulted when
    `settings.persistence_enabled` is true. An injected `writer` is still started
    and drained by the lifespan: running it is part of serving, unlike closing a
    connection someone else opened.
    """
    resolved_settings = settings or get_settings()
    injected_store = store
    injected_database = database
    injected_writer = writer

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

        # Persistence is optional and strictly additive: a database that cannot be set up
        # leaves the live dashboard running with no history, rather than no service.
        history_db: Database | None = None
        owns_database = False
        history_writer: CrossingWriter | None = None
        writer_task: asyncio.Task[None] | None = None
        if resolved_settings.persistence_enabled:
            try:
                history_db = injected_database
                if history_db is None:
                    history_db = Database.from_settings(resolved_settings)
                    owns_database = True
                history_writer = (
                    injected_writer
                    if injected_writer is not None
                    else CrossingWriter.from_settings(RepositorySink(history_db), resolved_settings)
                )
            except Exception as exc:
                # Bad URL, missing driver, invalid pool settings. Live-only from here on.
                logger.error("db.init_failed", error=str(exc))
                if owns_database and history_db is not None:
                    await history_db.close()
                history_db, history_writer, owns_database = None, None, False
        if history_writer is not None:
            # Started before the pipelines so the first crossing already has a consumer.
            writer_task = asyncio.create_task(_run_writer(history_writer))

        pipelines: list[CameraPipeline] = []
        tasks: list[asyncio.Task[None]] = []
        try:
            pipelines = _build_pipelines(
                pipeline_factory, resolved_settings, state_store, history_writer
            )
            tasks = [asyncio.create_task(_run_pipeline(p)) for p in pipelines]

            app.state.settings = resolved_settings
            app.state.store = state_store
            app.state.pipelines = pipelines
            app.state.pipeline_tasks = tasks
            app.state.database = history_db
            app.state.history = RepositoryHistory(history_db) if history_db is not None else None

            logger.info(
                "api.startup",
                camera_count=len(pipelines),
                persistence=history_db is not None,
            )
            yield
        finally:
            # Order matters: pipelines first, so nothing is still calling `submit()` when
            # the writer drains. Reversed, the final crossings land in a buffer nobody
            # flushes.
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
            if history_writer is not None and writer_task is not None:
                history_writer.request_stop()
                try:
                    await asyncio.wait_for(writer_task, timeout=_SHUTDOWN_TIMEOUT_SECONDS)
                except TimeoutError:
                    # `wait_for` has cancelled the task: whatever was still buffered is lost.
                    # Counted so the loss is visible rather than silent.
                    logger.warning(
                        "db.shutdown_drain_timeout", abandoned_events=history_writer.pending_count
                    )
            if owns_database and history_db is not None:
                await history_db.close()
            if owns_store:
                await state_store.close()
            logger.info("api.shutdown")

    app = FastAPI(lifespan=lifespan)

    # --- middleware stack ---------------------------------------------------
    # Wanted nesting, OUTERMOST first:
    #
    #   request-id -> metrics -> security headers -> rate limit -> auth -> routes
    #
    # Starlette's `add_middleware` (which `app.middleware("http")` calls)
    # inserts at the FRONT of the stack, so the LAST one registered is the
    # OUTERMOST. The registrations below are therefore written in the reverse of
    # the nesting above — auth first, request-id last. Reordering these calls
    # silently reorders the stack, so each carries its reason:
    #
    # - request-id is outermost so every log line in the request, including the
    #   401 and 429 that inner layers emit, carries the id; it also stamps the
    #   id on every response, rejected ones included.
    # - metrics sits just inside it so a request an inner layer rejects (401,
    #   429) is still counted and timed; a metrics layer inside auth would be
    #   blind to exactly the traffic an operator most wants to see.
    # - security headers sit outside rate limit and auth so those layers'
    #   error responses carry the headers too. They are not a feature of the
    #   happy path only.
    # - rate limit sits outside auth so an unauthenticated flood is throttled
    #   BEFORE it reaches the token check, rather than being free to hammer it.
    # - auth is innermost of the four: it only decides whether a request that
    #   survived everything above may reach a route.
    #
    # A disabled feature is not installed at all, rather than installed and
    # checking a flag on every request.
    if resolved_settings.auth_enabled:
        app.add_middleware(AuthMiddleware, settings=resolved_settings)
    if resolved_settings.rate_limit_enabled:
        app.add_middleware(RateLimitMiddleware, settings=resolved_settings)
    if resolved_settings.security_headers_enabled:
        app.add_middleware(SecurityHeadersMiddleware, settings=resolved_settings)
    if resolved_settings.metrics_enabled:
        app.middleware("http")(_metrics_middleware)
    app.middleware("http")(_request_id_middleware)

    app.include_router(router)

    if resolved_settings.metrics_enabled:
        # The routes live in `routes.py`, but the scrape path is a setting, so
        # it is registered here. It sits under `/api` and is not in
        # `auth_exempt_paths`, so when a token is configured `AuthMiddleware`
        # covers it like any other route: metrics reveal camera ids and traffic
        # volume, which are not for anonymous callers.
        app.add_api_route(
            resolved_settings.metrics_path,
            _metrics_endpoint,
            methods=["GET"],
            include_in_schema=False,
        )
    return app
