"""Shared fixtures.

Deliberately torch-free: the core suite must run on a laptop with no inference
stack installed. Anything needing ultralytics is marked `requires_inference` and
skipped unless it is importable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from traffic_ai.cameras import CAMERAS
from traffic_ai.config import Settings
from traffic_ai.domain import CameraState, PipelineStatus
from traffic_ai.store import StateStore


@pytest.fixture
def settings() -> Settings:
    """Settings with the environment ignored, so a developer's shell cannot
    change the outcome of a test run."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        redis_url="redis://localhost:6379/15",
        state_ttl_seconds=30,
        event_history=50,
        anpr_enabled=False,
    )


@pytest.fixture
async def store() -> StateStore:
    """StateStore backed by fakeredis — real client semantics, no server."""
    import fakeredis.aioredis

    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    s = StateStore(client, ttl_seconds=30, event_history=50)
    yield s
    await s.close()


@pytest.fixture
def camera():
    return CAMERAS[0]


@pytest.fixture
def running_state(camera) -> CameraState:
    return CameraState(
        camera_id=camera.camera_id,
        name=camera.name,
        status=PipelineStatus.RUNNING,
        updated_at=datetime.now(UTC),
    )


def pytest_collection_modifyitems(config, items):
    """Skip inference-dependent tests when the stack is not installed."""
    try:
        import ultralytics  # noqa: F401

        return
    except ImportError:
        skip = pytest.mark.skip(reason="ultralytics not installed")
        for item in items:
            if "requires_inference" in item.keywords:
                item.add_marker(skip)
