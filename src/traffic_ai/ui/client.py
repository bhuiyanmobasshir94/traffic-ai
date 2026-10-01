"""Typed HTTP client for the inference worker's `/api` surface.

The worker (`traffic_ai.worker`, built elsewhere) publishes `CameraSummary`,
`CameraState`, and `CrossingEvent` (`traffic_ai.domain`) over HTTP. This module
is the only place in the UI that speaks HTTP; every other module works with
those typed models, never with a raw dict or an `httpx.Response`.

A network failure never reaches the page as a traceback: a connection error or
timeout becomes `WorkerUnavailable`, and a 404/503 from a per-camera lookup
becomes `None` — "no data yet" is expected, not exceptional, and callers
branch on it rather than catching an exception for it.

When `Settings.api_token` is set, every server-side request carries it as a
bearer token. The token lives only inside the `httpx.Client` headers: it is not
stored on this object, not interpolated into any exception message, and never
rendered. A 401 is its own error (`ApiUnauthorized`) so a missing or wrong
token reads as exactly that rather than as "worker down". A 409 is likewise its own
error (`CountingNotCalibrated`): the camera is not calibrated for counting.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

import httpx
import streamlit as st
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from traffic_ai.cameras import DEFAULT_COUNTING_DISABLED_REASON
from traffic_ai.config import Settings, get_settings
from traffic_ai.domain import CameraState, CameraSummary, CrossingEvent, Direction

# A 404 means the camera id is unknown to the worker; a 503 means the worker
# is up but has not published anything for it yet. Both read as "no data".
_NOT_READY_STATUSES = (404, 503)

_UNAUTHORIZED_MESSAGE = "UI is not authorized to the API (check TRAFFIC_AI_API_TOKEN)"
_HISTORY_UNAVAILABLE_MESSAGE = (
    "History unavailable: persistence is disabled or the database cannot be reached"
)

_NonNegativeInt = Annotated[int, Field(ge=0)]


class WorkerUnavailable(Exception):
    """The worker could not be reached, or answered with an unexpected error."""


class ApiUnauthorized(WorkerUnavailable):
    """The API answered 401: the token is missing, wrong, or revoked.

    A subclass of `WorkerUnavailable` so a caller that only handles "the worker
    did not give me data" still degrades instead of crashing; a caller that
    cares (the Analytics page) catches this one first and says what is wrong.
    """


class HistoryUnavailable(WorkerUnavailable):
    """The API answered 503 on a history endpoint: persistence is off or the
    database is down. Live data is unaffected; only history is missing."""


class CountingNotCalibrated(WorkerUnavailable):
    """The API answered 409: counting is switched off for the camera asked about, so it
    serves no crossings, live or historical, for it. Not a fault and not "no data yet".

    The message is fixed rather than the response's `detail`: nothing the worker sends is
    rendered verbatim. The registry (`CameraConfig.counting_disabled_reason`) is where a
    page gets the camera-specific wording.
    """


# --- history results ---------------------------------------------------------
# Local to the UI on purpose: `domain.py` is the worker/UI contract and these
# are only the shapes of the history endpoints' envelopes. `CrossingEvent`
# itself is the domain model and is not redefined here.


class HistoryCounts(BaseModel):
    """`GET /history/counts`: crossings in a window, by direction and class."""

    model_config = ConfigDict(frozen=True)

    camera_id: str | None = None
    since: datetime
    until: datetime
    counts: dict[Direction, dict[str, _NonNegativeInt]]
    total: _NonNegativeInt


class HourlyBucket(BaseModel):
    model_config = ConfigDict(frozen=True)

    hour: datetime
    total: _NonNegativeInt


class HistoryHourly(BaseModel):
    """`GET /history/hourly`: crossings per hour across a window."""

    model_config = ConfigDict(frozen=True)

    camera_id: str | None = None
    since: datetime
    until: datetime
    buckets: list[HourlyBucket]


_EVENTS_ADAPTER = TypeAdapter(list[CrossingEvent])


def _checked_token(token: str) -> str:
    """Accept only a token that can be sent as a header value verbatim.

    A value with a newline or other control character is rejected by the HTTP
    layer at send time, and its error message quotes the whole header — token
    included. Refusing it here, with a fixed message, keeps the token out of
    every exception this client can raise.
    """
    if not all(0x21 <= ord(ch) <= 0x7E for ch in token):
        raise ValueError(
            "TRAFFIC_AI_API_TOKEN must be visible ASCII with no whitespace or control "
            "characters (a trailing newline from a secret file is the usual cause)"
        )
    return token


def _to_utc_iso(moment: datetime) -> str:
    # A naive datetime has no defined meaning on the wire; refuse it rather
    # than let the server guess a timezone and silently shift the window.
    if moment.tzinfo is None:
        raise ValueError("history windows require timezone-aware datetimes")
    return moment.astimezone(UTC).isoformat()


def _window_params(camera_id: str | None, since: datetime, until: datetime) -> dict[str, object]:
    params: dict[str, object] = {"since": _to_utc_iso(since), "until": _to_utc_iso(until)}
    if camera_id:
        params["camera_id"] = camera_id
    return params


class WorkerClient:
    """Thin, typed wrapper around the worker's `/api` endpoints.

    `base_url` is the server-side address this client actually connects to
    (`Settings.api_internal_url` + `/api`). `public_base_url` is what
    `stream_url` hands to the browser (`Settings.api_public_url`) — the two
    differ because the UI process and the browser reach the worker through
    different routes (compose network vs. Traefik).

    `api_token`, when given, is sent as `Authorization: Bearer ...` on every
    request this client makes. It is deliberately not kept as an attribute.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        *,
        public_base_url: str | None = None,
        api_token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._public_base_url = (public_base_url or base_url).rstrip("/")
        headers = {"Authorization": f"Bearer {_checked_token(api_token)}"} if api_token else None
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers=headers,
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

    # --- history (Postgres-backed) ------------------------------------
    # Unlike the live per-camera lookups, a 503 here is not "no data yet": it
    # means history is switched off or the database is down, and the caller
    # must be able to say so. Hence `HistoryUnavailable` rather than `None`.

    def history_events(
        self, camera_id: str | None = None, *, limit: int = 100
    ) -> list[CrossingEvent]:
        """Most recent persisted crossings, newest first as the API orders them.
        Not window-bound: the endpoint takes a limit, not a time range."""
        params: dict[str, object] = {"limit": limit}
        if camera_id:
            params["camera_id"] = camera_id
        return self._get_history("/history/events", params, _EVENTS_ADAPTER.validate_python)

    def history_counts(
        self, *, since: datetime, until: datetime, camera_id: str | None = None
    ) -> HistoryCounts:
        params = _window_params(camera_id, since, until)
        return self._get_history("/history/counts", params, HistoryCounts.model_validate)

    def history_hourly(
        self, *, since: datetime, until: datetime, camera_id: str | None = None
    ) -> HistoryHourly:
        params = _window_params(camera_id, since, until)
        return self._get_history("/history/hourly", params, HistoryHourly.model_validate)

    def history_events_lost(self) -> int | None:
        """Crossings the worker counted but failed to persist, from `/readyz`.

        `None` means unknown: persistence is off, the worker did not say, or it could
        not be asked. Never raises -- this only feeds an advisory warning, so a failure
        to ask must not take the page it decorates down. `/readyz` answers 503 when the
        worker is not ready but still carries the body, so the status code is not
        checked; and it is an auth-exempt path, so a bad token cannot reach this.
        """
        try:
            body = self._client.get("/readyz").json()
        except (httpx.HTTPError, ValueError):
            return None
        lost = body.get("history_events_lost") if isinstance(body, dict) else None
        # `bool` is an `int`, and a negative count is not a count.
        if isinstance(lost, int) and not isinstance(lost, bool) and lost >= 0:
            return lost
        return None

    def healthy(self) -> bool:
        """Liveness probe for the status banner. Never raises."""
        try:
            response = self._client.get("/healthz")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def stream_url(self, camera_id: str) -> str:
        """Public (browser-facing) URL for the annotated MJPEG stream."""
        # A browser <img> cannot send a header; edge auth (Traefik/ingress) injects
        # the token, so it is never placed in this URL.
        return f"{self._public_base_url}/cameras/{camera_id}/stream.mjpg"

    # --- internals ----------------------------------------------------

    def _request(self, path: str, *, params: dict[str, object] | None = None) -> httpx.Response:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise WorkerUnavailable(f"could not reach worker at {path}: {exc}") from exc
        if response.status_code == 401:
            raise ApiUnauthorized(_UNAUTHORIZED_MESSAGE)
        # The only 409 the API returns: a per-camera crossings or history route asked
        # about a camera whose counting is off. Without this it would surface as a generic
        # "worker returned 409", which the live region reports as an unreachable worker.
        if response.status_code == 409:
            raise CountingNotCalibrated(DEFAULT_COUNTING_DISABLED_REASON)
        return response

    def _get_json(self, path: str, *, params: dict[str, object] | None = None) -> object:
        response = self._request(path, params=params)
        if response.is_error:
            raise WorkerUnavailable(f"worker returned {response.status_code} for {path}")
        return response.json()

    def _get_history[T](
        self, path: str, params: dict[str, object], parse: Callable[[object], T]
    ) -> T:
        response = self._request(path, params=params)
        if response.status_code == 503:
            raise HistoryUnavailable(_HISTORY_UNAVAILABLE_MESSAGE)
        if response.is_error:
            raise WorkerUnavailable(f"worker returned {response.status_code} for {path}")
        try:
            return parse(response.json())
        except ValueError as exc:  # JSON decode and pydantic validation errors both subclass it
            raise WorkerUnavailable(f"worker returned an unexpected body for {path}") from exc


def build_worker_client(
    settings: Settings, *, transport: httpx.BaseTransport | None = None
) -> WorkerClient:
    """The one place `Settings` becomes a `WorkerClient`, token included."""
    token = settings.api_token.get_secret_value() if settings.api_token is not None else None
    return WorkerClient(
        base_url=f"{settings.api_internal_url}/api",
        timeout=settings.api_timeout_seconds,
        public_base_url=settings.api_public_url,
        api_token=token,
        transport=transport,
    )


@st.cache_resource
def get_worker_client() -> WorkerClient:
    """Process-wide client, cached so the connection pool survives reruns
    instead of being rebuilt on every script execution."""
    return build_worker_client(get_settings())
