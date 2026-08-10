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
    `settings_override=...` to change any of them."""

    def build(
        *,
        pipelines: list[CameraPipeline] | None = None,
        store_override: object | None = None,
        settings_override: Settings | None = None,
    ) -> FastAPI:
        return create_app(
            settings=settings_override or settings,
            store=store_override or store,  # type: ignore[arg-type]
            pipeline_factory=_pipeline_factory_with(*(pipelines or [])),
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
