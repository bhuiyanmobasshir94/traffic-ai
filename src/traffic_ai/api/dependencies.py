"""Shared FastAPI dependencies: settings, the store, and the camera allowlist.

Each dependency reads from the running app's `request.app.state` rather than a
process-wide singleton, so tests can inject a fake store and a stub settings
object without monkeypatching module globals.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from traffic_ai.cameras import CameraConfig, get_camera
from traffic_ai.config import Settings
from traffic_ai.store import StateStore


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
