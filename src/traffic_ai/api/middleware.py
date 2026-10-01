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

import ipaddress
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

# Ceiling on distinct (client, kind) buckets held at once. Legitimate traffic is a
# handful of addresses; this exists for the case where the key is attacker-chosen
# (a mis-set `trusted_proxy_hops`, a worker reachable without its proxy). 10k small
# entries is about a megabyte.
_MAX_BUCKETS = 10_000


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

    Two kinds of request never spend the ordinary budget, because limiting them
    only does harm:

    - The auth-exempt probe paths (`auth_exempt_paths`, health and readiness).
      An orchestrator that gets a 429 from its liveness probe restarts a
      healthy container; the probes are cheap and carry no data.
    - Requests that present a VALID service bearer token. The Streamlit UI calls
      this API server-to-server, so every viewer's requests arrive from the
      UI container's one address and share one bucket -- a few viewers polling
      every couple of seconds exceed the default budget, and the UI reports a
      healthy worker as unavailable. The token is the credential: a caller that
      has it is trusted, and throttling it protects nothing. A request with a
      WRONG or missing token is still counted, so the unauthenticated flood this
      layer exists for is throttled before it reaches `AuthMiddleware`.

    The stream budget applies to token holders too: a browser's stream request
    can carry the token (the edge injects it), and holding many long-lived
    streams is the thing that budget is there to stop.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self._requests = settings.rate_limit_requests
        self._stream_requests = settings.rate_limit_stream_requests
        self._window = settings.rate_limit_window_seconds
        self._trusted_hops = settings.trusted_proxy_hops
        self._exempt_paths = settings.auth_exempt_paths
        # Only to recognise a trusted caller; deciding whether a request is
        # AUTHORISED stays with `AuthMiddleware`.
        self._token = settings.api_token.get_secret_value() if settings.api_token else None
        self._buckets: dict[tuple[str, bool], _Bucket] = {}
        self._last_sweep = time.monotonic()

    def client_key(self, request: Request) -> str:
        """The address a request is attributed to.

        The socket peer, unless `trusted_proxy_hops` says a proxy sits in front. Behind
        Traefik the peer is always the proxy itself, so without `X-Forwarded-For` every
        user would share one bucket. Each proxy appends the address it received the
        request from, so with N trusted proxies the client is the entry N places from
        the RIGHT; everything left of that was written by the caller (or by something
        earlier in the chain) and is attacker-controlled. Taking the leftmost entry --
        the original rule -- lets any caller mint a fresh bucket per request by sending
        `X-Forwarded-For: <random>`, which defeats the limit entirely.

        Falls back to the socket peer, never to a header value, whenever the header
        cannot be trusted: hops is 0, the header is absent, it has fewer entries than
        there are trusted proxies (the request did not come through them), or the
        selected entry is not an IP address. The peer is a shared bucket, which is the
        strict way to be wrong.
        """
        peer = request.client.host if request.client else "unknown"
        hops = self._trusted_hops
        forwarded = request.headers.get("x-forwarded-for")
        if hops == 0 or not forwarded:
            return peer
        entries = forwarded.split(",")
        if len(entries) < hops:
            return peer
        try:
            # Normalised, so `::1` and `0:0:0:0:0:0:0:1` are one client.
            return str(ipaddress.ip_address(entries[-hops].strip()))
        except ValueError:
            return peer

    def _is_exempt(self, request: Request, *, is_stream: bool) -> bool:
        """True for a request that must not be counted against the ordinary budget."""
        if security.is_path_exempt(request.url.path, self._exempt_paths):
            return True
        if is_stream or self._token is None:
            return False
        provided = security.extract_bearer_token(request.headers.get("authorization"))
        return provided is not None and security.tokens_match(provided, self._token)

    def _sweep(self, now: float) -> None:
        """Drop buckets whose window has fully elapsed.

        An expired bucket is indistinguishable from no bucket — the next
        request would reset it anyway — so evicting it changes no decision and
        simply stops the dict growing by one entry per address ever seen. Runs
        at most once per window, so the O(n) scan is amortised. Between sweeps
        the dict is capped by `_MAX_BUCKETS` (see `dispatch`), so a caller that
        can choose its own key cannot grow memory without bound.
        """
        if now - self._last_sweep < self._window:
            return
        self._last_sweep = now
        expired = [k for k, b in self._buckets.items() if now - b.window_start >= self._window]
        for key in expired:
            del self._buckets[key]

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        is_stream = request.url.path.endswith(_STREAM_PATH_SUFFIX)
        if self._is_exempt(request, is_stream=is_stream):
            return await call_next(request)

        # Everything up to `call_next` is synchronous, so two coroutines can
        # never interleave inside the check-and-increment: no lock needed.
        now = time.monotonic()
        self._sweep(now)

        limit = self._stream_requests if is_stream else self._requests
        key = (self.client_key(request), is_stream)

        bucket = self._buckets.get(key)
        if bucket is None or now - bucket.window_start >= self._window:
            # Re-inserted rather than reset in place, so dict order stays window-start
            # order and the first key is always the oldest window.
            self._buckets.pop(key, None)
            if len(self._buckets) >= _MAX_BUCKETS:
                # Memory bound against key spraying. Evicting the oldest window gives
                # that one client a fresh budget; the alternative is unbounded growth.
                del self._buckets[next(iter(self._buckets))]
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
