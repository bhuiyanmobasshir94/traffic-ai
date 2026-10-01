"""Edge-hardening middleware: security headers, rate limiting, bearer auth.

Each class is a Starlette `BaseHTTPMiddleware` — the same machinery behind
`app.middleware("http")` that `traffic_ai.api.app._request_id_middleware`
already uses — written as a class only because each one carries configuration
and, for the rate limiter, state. `create_app` decides whether each is installed
at all (a disabled feature adds no middleware, rather than adding one that
checks a flag on every request) and, just as important, the ORDER they are
installed in; the ordering rationale lives next to the wiring in `app.py`
because it is a property of the whole stack, not of any one class.

RATE LIMITING IS PER PROCESS. `RateLimitMiddleware` keeps its counters in the
memory of the worker process that serves the request. That is a real
limitation, not a detail:

- Run N replicas (or N uvicorn workers) and each keeps its own counters, so
  the effective limit for one client is up to N times the configured one,
  depending on how the load balancer spreads that client's requests.
- A restart forgets every counter, so a client at its limit gets a fresh
  budget.

A shared limiter (Redis `INCR` + `EXPIRE`, or the proxy's own rate limiting)
would fix both, and is the right move once this runs as more than one
replica. It is not done here because the current deployment is a single
worker container and a Redis round-trip on every request is a cost this stack
does not yet need to pay. Treat this limiter as protection against a runaway
client or a casual scraper, not as a hard quota.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from traffic_ai.api import security
from traffic_ai.config import Settings
from traffic_ai.logging import get_logger

logger = get_logger(__name__)

CallNext = Callable[[Request], Awaitable[Response]]

# The MJPEG stream is the only route counted against the tighter stream limit.
# Matching the suffix (rather than resolving the route) is deliberate: rate
# limiting runs BEFORE routing, so `scope["route"]` does not exist yet.
_STREAM_PATH_SUFFIX = "/stream.mjpg"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add the standard response-hardening headers to every response.

    Installed OUTSIDE rate limiting and auth (see `create_app`), so the headers
    are present on 401 and 429 responses too — an error page is not exempt from
    `nosniff` or framing protection.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self._hsts_value: str | None = (
            f"max-age={settings.hsts_max_age_seconds}; includeSubDomains"
            if settings.is_production
            else None
        )

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        response = await call_next(request)
        headers = response.headers
        # Stop browsers MIME-sniffing a JSON or image response into something
        # executable.
        headers["X-Content-Type-Options"] = "nosniff"
        # Nothing here is meant to be framed; this closes clickjacking.
        headers["X-Frame-Options"] = "DENY"
        # Do not leak the (possibly internal) URL of this API to third parties
        # through the Referer header.
        headers["Referrer-Policy"] = "no-referrer"
        # The API serves JSON, JPEGs and an MJPEG stream, never a page that
        # needs a sensor; deny the powerful features outright.
        headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # This service returns data, not documents, so there is no script,
        # style or image that a response legitimately needs to load. A policy of
        # "load nothing, be framed by nobody" costs nothing and means a
        # response that is ever rendered as HTML can do nothing.
        headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        # HSTS only in production. Sent from a plain-HTTP development server it
        # is ignored by browsers over HTTP, but the same hostname later served
        # over HTTPS would be pinned for max-age (a year by default) — and a
        # developer who hit `localhost` or a shared dev hostname would find that
        # whole name stuck on HTTPS with no easy undo. Production is the only
        # place that is certainly behind TLS and certainly means it.
        if self._hsts_value is not None:
            headers["Strict-Transport-Security"] = self._hsts_value
        return response


@dataclass(slots=True)
class _Bucket:
    window_start: float
    count: int


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window rate limit per client IP — IN-PROCESS, see module docstring.

    Two budgets share one window length: ordinary requests count against
    `rate_limit_requests`, and the MJPEG stream against the much smaller
    `rate_limit_stream_requests` (each viewer holds one long-lived request, so
    a handful per window is already a lot). They are tracked separately so a
    client polling JSON cannot spend its stream budget, or the reverse.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self._requests = settings.rate_limit_requests
        self._stream_requests = settings.rate_limit_stream_requests
        self._window = settings.rate_limit_window_seconds
        self._buckets: dict[tuple[str, bool], _Bucket] = {}
        self._last_sweep = time.monotonic()

    @staticmethod
    def client_key(request: Request) -> str:
        """The address a request is attributed to.

        `X-Forwarded-For` is honoured ONLY when present, and the LEFTMOST entry
        is used. Behind Traefik the socket peer is always the proxy itself, so
        without this every user would share one bucket. Each proxy hop appends
        the address it received the request from, so the first entry is the
        original client and the last is the nearest proxy.

        That header is client-controlled. If this app is ever reachable without
        a proxy in front (a published port, a misconfigured network), any caller
        can send `X-Forwarded-For: <random>` and get a fresh bucket per request,
        defeating the limit entirely. The deployment keeps the worker off the
        host network so only Traefik can reach it; if that changes, trust only
        a rightmost-N-hops count of known proxies instead.
        """
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
        return request.client.host if request.client else "unknown"

    def _sweep(self, now: float) -> None:
        """Drop buckets whose window has fully elapsed.

        An expired bucket is indistinguishable from no bucket — the next
        request would reset it anyway — so evicting it changes no decision and
        simply stops the dict growing by one entry per address ever seen. Runs
        at most once per window, so the O(n) scan is amortised. Memory is
        therefore bounded by (request rate x window), and an `X-Forwarded-For`
        spoofer can inflate that, which is one more reason the header is only
        safe behind a proxy.
        """
        if now - self._last_sweep < self._window:
            return
        self._last_sweep = now
        expired = [k for k, b in self._buckets.items() if now - b.window_start >= self._window]
        for key in expired:
            del self._buckets[key]

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        # Everything up to `call_next` is synchronous, so two coroutines can
        # never interleave inside the check-and-increment: no lock needed.
        now = time.monotonic()
        self._sweep(now)

        is_stream = request.url.path.endswith(_STREAM_PATH_SUFFIX)
        limit = self._stream_requests if is_stream else self._requests
        key = (self.client_key(request), is_stream)

        bucket = self._buckets.get(key)
        if bucket is None or now - bucket.window_start >= self._window:
            bucket = _Bucket(window_start=now, count=0)
            self._buckets[key] = bucket
        bucket.count += 1

        if bucket.count > limit:
            retry_after = max(1, math.ceil(self._window - (now - bucket.window_start)))
            # Log once per window per client, not once per rejected request: a
            # client hammering past its limit must not also flood the log.
            if bucket.count == limit + 1:
                logger.warning(
                    "rate_limit.exceeded",
                    path=request.url.path,
                    limit=limit,
                    retry_after=retry_after,
                )
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)


class AuthMiddleware(BaseHTTPMiddleware):
    """Require `Authorization: Bearer <api_token>` on every non-exempt path.

    Middleware rather than per-route dependencies so one rule covers every
    route — including the metrics endpoint and the MJPEG stream — and a route
    added later cannot forget it. Fails closed: anything that is not a valid
    token, including a malformed header, is a 401 and never a 500.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        if settings.api_token is None:
            # Constructing an auth layer with nothing to check against would
            # silently let everyone in. `create_app` never does this; refuse
            # loudly if someone else does.
            raise ValueError("AuthMiddleware requires settings.api_token to be set")
        self._token = settings.api_token.get_secret_value()
        self._exempt_paths = settings.auth_exempt_paths

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        header = request.headers.get("authorization")
        if security.is_authorized(
            path=request.url.path,
            authorization_header=header,
            api_token=self._token,
            exempt_paths=self._exempt_paths,
        ):
            return await call_next(request)

        # Log that a request was refused and whether it carried any credential
        # at all — never the credential. The body is identical for "missing",
        # "malformed" and "wrong", so a caller learns nothing about which part
        # of their attempt was close.
        logger.warning(
            "auth.rejected",
            method=request.method,
            path=request.url.path,
            credential_supplied=header is not None,
        )
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )
