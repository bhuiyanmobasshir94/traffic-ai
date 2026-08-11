"""Typed HTTP client for the inference worker's `/api` surface.

The worker (`traffic_ai.worker`, built elsewhere) publishes `CameraSummary`,
`CameraState`, and `CrossingEvent` (`traffic_ai.domain`) over HTTP. This module
is the only place in the UI that speaks HTTP; every other module works with
those typed models, never with a raw dict or an `httpx.Response`.

A network failure never reaches the page as a traceback: a connection error or
timeout becomes `WorkerUnavailable`, and a 404/503 from a per-camera lookup
becomes `None` — "no data yet" is expected, not exceptional, and callers
branch on it rather than catching an exception for it.
"""

from __future__ import annotations

import httpx
import streamlit as st

from traffic_ai.config import get_settings
from traffic_ai.domain import CameraState, CameraSummary, CrossingEvent

# A 404 means the camera id is unknown to the worker; a 503 means the worker
# is up but has not published anything for it yet. Both read as "no data".
_NOT_READY_STATUSES = (404, 503)


class WorkerUnavailable(Exception):
    """The worker could not be reached, or answered with an unexpected error."""


class WorkerClient:
    """Thin, typed wrapper around the worker's `/api` endpoints.

    `base_url` is the server-side address this client actually connects to
    (`Settings.api_internal_url` + `/api`). `public_base_url` is what
    `stream_url` hands to the browser (`Settings.api_public_url`) — the two
    differ because the UI process and the browser reach the worker through
    different routes (compose network vs. Traefik).
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        *,
        public_base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._public_base_url = (public_base_url or base_url).rstrip("/")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def cameras(self) -> list[CameraSummary]:
        data = self._get_json("/cameras")
        return [CameraSummary.model_validate(item) for item in data]

    def state(self, camera_id: str) -> CameraState | None:
        response = self._request(f"/cameras/{camera_id}/state")
        if response.status_code in _NOT_READY_STATUSES:
            return None
        if response.is_error:
            raise WorkerUnavailable(
                f"worker returned {response.status_code} for camera {camera_id!r} state"
            )
        return CameraState.model_validate(response.json())

    def states(self) -> dict[str, CameraState]:
        """State for every known camera. Cameras with no data yet are absent,
        not `None`-valued — callers iterate the result rather than checking it."""
        out: dict[str, CameraState] = {}
        for summary in self.cameras():
            state = self.state(summary.camera_id)
            if state is not None:
                out[summary.camera_id] = state
        return out

    def events(self, camera_id: str | None = None, limit: int = 25) -> list[CrossingEvent]:
        path = f"/cameras/{camera_id}/events" if camera_id else "/events"
        data = self._get_json(path, params={"limit": limit})
        return [CrossingEvent.model_validate(item) for item in data]

    def healthy(self) -> bool:
        """Liveness probe for the status banner. Never raises."""
        try:
            response = self._client.get("/healthz")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def stream_url(self, camera_id: str) -> str:
        """Public (browser-facing) URL for the annotated MJPEG stream."""
        return f"{self._public_base_url}/cameras/{camera_id}/stream.mjpg"

    # --- internals ----------------------------------------------------

    def _request(self, path: str, *, params: dict[str, object] | None = None) -> httpx.Response:
        try:
            return self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise WorkerUnavailable(f"could not reach worker at {path}: {exc}") from exc

    def _get_json(self, path: str, *, params: dict[str, object] | None = None) -> object:
        response = self._request(path, params=params)
        if response.is_error:
            raise WorkerUnavailable(f"worker returned {response.status_code} for {path}")
        return response.json()


@st.cache_resource
def get_worker_client() -> WorkerClient:
    """Process-wide client, cached so the connection pool survives reruns
    instead of being rebuilt on every script execution."""
    settings = get_settings()
    return WorkerClient(
        base_url=f"{settings.api_internal_url}/api",
        timeout=settings.api_timeout_seconds,
        public_base_url=settings.api_public_url,
    )
