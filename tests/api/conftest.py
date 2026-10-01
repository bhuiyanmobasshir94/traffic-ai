"""Fixtures for the API test suite.

Everything here runs the ASGI app through `httpx.ASGITransport` on the same
event loop as the test coroutine — never Starlette's `TestClient`, which drives
requests from a background-thread event loop. The fakeredis client behind the
`store` fixture (see `tests/conftest.py`) binds its internal asyncio
primitives to whichever loop first touches it; splitting the app and the test
body across two loops is a real source of "attached to a different loop"
flakiness, not a hypothetical one, so `running_app` below keeps both on one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from traffic_ai.api.app import CameraPipeline, PipelineFactory, create_app
from traffic_ai.config import Settings
from traffic_ai.domain import CameraState, PipelineStatus
from traffic_ai.store import StateStore


class StubPipeline:
    """A pipeline double: a controllable status, and a `run()` that blocks
    until `request_stop()` is called — never a real inference loop."""

    def __init__(self, camera_id: str, status: PipelineStatus = PipelineStatus.RUNNING) -> None:
        self._camera_id = camera_id
        self._status = status
        self._stop_event = asyncio.Event()

    @property
    def camera_id(self) -> str:
        return self._camera_id

    @property
    def state(self) -> CameraState:
        return CameraState(
            camera_id=self._camera_id,
            name=self._camera_id,
            status=self._status,
            updated_at=datetime.now(UTC),
        )

    async def run(self) -> None:
        await self._stop_event.wait()

    def request_stop(self) -> None:
        self._stop_event.set()


class CrashingPipeline:
    """A pipeline double whose `run()` raises immediately, proving the
    lifespan logs a crashed task instead of letting it kill the process."""

    camera_id = "crashing"

    @property
    def state(self) -> CameraState:
        return CameraState(
            camera_id=self.camera_id,
            name=self.camera_id,
            status=PipelineStatus.ERROR,
            updated_at=datetime.now(UTC),
        )

    async def run(self) -> None:
        raise RuntimeError("boom")

    def request_stop(self) -> None:
        return None


class BrokenStore:
    """Stands in for a `StateStore` whose Redis is unreachable. Only `ping`
    and `close` are implemented — that is all the readiness path calls."""

    async def ping(self) -> bool:
        return False

    async def close(self) -> None:
        return None


def _pipeline_factory_with(*pipelines: CameraPipeline) -> PipelineFactory:
    def factory(settings: Settings, store: StateStore) -> list[CameraPipeline]:
        return list(pipelines)

    return factory


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    """Build `Settings` with explicit overrides, ignoring the environment.

    The hardening tests flip auth, rate limiting, and the environment on and
    off, so each pins the fields it depends on rather than inheriting whatever
    the developer's shell exports. `api_token` and `environment` are pinned to
    their off/development values here for the same reason: an exported
    `TRAFFIC_AI_API_TOKEN` must not turn the unauthenticated tests into 401s.
    """

    def build(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "redis_url": "redis://localhost:6379/15",
            "state_ttl_seconds": 30,
            "event_history": 50,
            "anpr_enabled": False,
            "api_token": None,
            "environment": "development",
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)  # type: ignore[call-arg]

    return build


@pytest.fixture
def make_pipeline() -> Callable[..., StubPipeline]:
    return StubPipeline


@pytest.fixture
def crashing_pipeline() -> CrashingPipeline:
    return CrashingPipeline()


@pytest.fixture
def broken_store() -> BrokenStore:
    return BrokenStore()


@pytest.fixture
def app_factory(settings: Settings, store: StateStore) -> Callable[..., FastAPI]:
    """Build an app wired to the test `settings` and `store` fixtures by
    default. Pass `pipelines=[...]`, `store_override=...`, or
    `settings_override=...` to change any of them.

    Persistence is OFF unless asked for. `Settings.persistence_enabled` defaults to
    True, which would point every unrelated test's lifespan at the development
    database URL (`postgres:5432`) and make readiness probes depend on whether that
    hostname resolves. Pass `persistence=True`, or inject a `database`/`writer`, to
    opt in. `pipeline_factory` replaces the default factory when a test needs to see
    the `writer` the lifespan hands out.
    """

    def build(
        *,
        pipelines: list[CameraPipeline] | None = None,
        store_override: object | None = None,
        settings_override: Settings | None = None,
        persistence: bool = False,
        database: object | None = None,
        writer: object | None = None,
        pipeline_factory: PipelineFactory | None = None,
    ) -> FastAPI:
        resolved = settings_override or settings
        wants_persistence = persistence or database is not None or writer is not None
        if not wants_persistence:
            resolved = resolved.model_copy(update={"persistence_enabled": False})
        return create_app(
            settings=resolved,
            store=store_override or store,  # type: ignore[arg-type]
            pipeline_factory=pipeline_factory or _pipeline_factory_with(*(pipelines or [])),
            database=database,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
        )

    return build


@asynccontextmanager
async def _running_app(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run `app`'s lifespan and yield an `AsyncClient` bound to it, all on the
    caller's current event loop."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.fixture
def running_app() -> Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]:
    """`async with running_app(app) as client: ...` — app lifespan + an
    AsyncClient, both on this test's event loop. Exposed as a fixture (rather
    than imported directly) since sibling test modules have no `__init__.py`
    and are not import-addressable as a package."""
    return _running_app
