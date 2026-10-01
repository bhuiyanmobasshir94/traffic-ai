"""Security headers, rate limiting, and how the whole middleware stack nests."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

from traffic_ai.api import middleware as middleware_module
from traffic_ai.api.middleware import AuthMiddleware, RateLimitMiddleware, SecurityHeadersMiddleware
from traffic_ai.config import Settings

TOKEN = "correct-horse-battery-staple-0123456789"  # noqa: S105 - a test fixture, not a credential

# Settings that satisfy the production validator without needing a real
# database or a token, for tests that only care about HSTS.
PRODUCTION = {
    "environment": "production",
    "allow_unauthenticated": True,
    "persistence_enabled": False,
}

EXPECTED_STATIC_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
}


def _stack_names(app: FastAPI) -> list[str]:
    """Outermost-first names of the user middleware on `app`."""
    names = []
    for entry in app.user_middleware:
        if entry.cls is BaseHTTPMiddleware:
            names.append(entry.kwargs["dispatch"].__name__)
        else:
            names.append(entry.cls.__name__)
    return names


# --- security headers --------------------------------------------------------


async def test_every_security_header_is_present(app_factory, running_app) -> None:
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get("/api/healthz")
    for name, value in EXPECTED_STATIC_HEADERS.items():
        assert resp.headers[name] == value, name


async def test_hsts_absent_in_development(app_factory, running_app) -> None:
    """HSTS from a plain-HTTP dev server would pin the developer's hostname."""
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get("/api/healthz")
    assert "strict-transport-security" not in resp.headers


async def test_hsts_present_in_production(app_factory, running_app, settings_factory) -> None:
    app = app_factory(
        settings_override=settings_factory(hsts_max_age_seconds=12345, **PRODUCTION),
    )
    async with running_app(app) as client:
        resp = await client.get("/api/healthz")
    assert resp.headers["strict-transport-security"] == "max-age=12345; includeSubDomains"
    # Production still carries every other header.
    for name, value in EXPECTED_STATIC_HEADERS.items():
        assert resp.headers[name] == value, name


async def test_security_headers_on_error_responses(
    app_factory, running_app, settings_factory
) -> None:
    """Headers must be present on 401 and 404 as well as on success."""
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    async with running_app(app) as client:
        unauthorized = await client.get("/api/cameras")
        not_found = await client.get(
            "/api/cameras/nope/events", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert unauthorized.status_code == 401
    assert not_found.status_code == 404
    for resp in (unauthorized, not_found):
        for name, value in EXPECTED_STATIC_HEADERS.items():
            assert resp.headers[name] == value, (resp.status_code, name)


async def test_security_headers_disabled_means_not_installed(
    app_factory, running_app, settings_factory
) -> None:
    app = app_factory(settings_override=settings_factory(security_headers_enabled=False))
    assert SecurityHeadersMiddleware not in [m.cls for m in app.user_middleware]
    async with running_app(app) as client:
        resp = await client.get("/api/healthz")
    for name in EXPECTED_STATIC_HEADERS:
        assert name not in resp.headers


# --- rate limiting -----------------------------------------------------------


async def test_429_after_the_limit_with_retry_after(
    app_factory, running_app, settings_factory
) -> None:
    app = app_factory(
        settings_override=settings_factory(rate_limit_requests=3, rate_limit_window_seconds=60.0)
    )
    async with running_app(app) as client:
        statuses = [(await client.get("/api/cameras")).status_code for _ in range(3)]
        limited = await client.get("/api/cameras")
    assert statuses == [200, 200, 200]
    assert limited.status_code == 429
    retry_after = int(limited.headers["retry-after"])
    assert 1 <= retry_after <= 60
    assert limited.json() == {"detail": "rate limit exceeded"}


async def test_health_probes_are_never_rate_limited(
    app_factory, running_app, settings_factory, make_pipeline
) -> None:
    """REVERSED from `test_limit_applies_to_health_probes_too`, which pinned the opposite:
    that an auth-exempt probe was throttled "which is what you want from a flood of
    probes". It is not. An orchestrator that gets a 429 from its liveness or readiness
    probe restarts a healthy container or pulls it from rotation, so a busy client on the
    same address (the UI, which shares one) could take the service down. The probes are
    cheap and carry no data; the paths in `auth_exempt_paths` are exempt from the
    limiter as well as from auth, and do not spend the budget other requests draw on."""
    app = app_factory(
        pipelines=[make_pipeline("toll-plaza-a")],
        settings_override=settings_factory(rate_limit_requests=2),
    )
    async with running_app(app) as client:
        health = [(await client.get("/api/healthz")).status_code for _ in range(6)]
        ready = [(await client.get("/api/readyz")).status_code for _ in range(6)]
        # Not one of those probes was counted: the whole budget is still there.
        ordinary = [(await client.get("/api/cameras")).status_code for _ in range(3)]
    assert health == [200] * 6
    assert ready == [200] * 6
    assert ordinary == [200, 200, 429]


# --- the trusted UI is not throttled ---------------------------------------------


async def test_a_valid_bearer_token_is_exempt_from_the_ordinary_limit(
    app_factory, running_app, settings_factory
) -> None:
    app = app_factory(settings_override=settings_factory(api_token=TOKEN, rate_limit_requests=2))
    async with running_app(app) as client:
        authed = [
            (
                await client.get("/api/cameras", headers={"Authorization": f"Bearer {TOKEN}"})
            ).status_code
            for _ in range(8)
        ]
    assert authed == [200] * 8


async def test_three_viewers_polling_through_the_ui_are_not_throttled(
    app_factory, running_app, settings_factory
) -> None:
    """The reported failure: the UI calls the worker server-to-server, ~90 requests a
    minute per open viewer, all from the one UI container address -- one bucket. Two
    viewers already exceed the 120/min default, and the UI then reported a healthy worker
    as unavailable. Three viewers for a full minute, bearer token on every request."""
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))  # default limits
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with running_app(app) as client:
        codes = [
            (await client.get("/api/cameras", headers=headers)).status_code for _ in range(270)
        ]
    assert 429 not in codes
    assert codes == [200] * 270


async def test_the_same_traffic_without_a_valid_token_is_throttled(
    app_factory, running_app, settings_factory
) -> None:
    """Control for the test above: it only means something if this volume WOULD be
    limited, and a request with a missing or wrong token still is -- the exemption is
    the credential, not the address."""
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    async with running_app(app) as client:
        anonymous = [(await client.get("/api/cameras")).status_code for _ in range(270)]
        wrong = [
            (
                await client.get("/api/cameras", headers={"Authorization": "Bearer not-the-token"})
            ).status_code
            for _ in range(3)
        ]
    assert anonymous[:120] == [401] * 120
    assert set(anonymous[120:]) == {429}
    # Same address, same bucket, already spent -- a wrong token does not buy a way out.
    assert wrong == [429, 429, 429]


@pytest.mark.parametrize(
    "authorization", ["Bearer ", "Bearer", "Basic dXNlcjpwYXNz", "bearer-but-not", ""]
)
async def test_a_malformed_or_foreign_credential_gets_no_exemption(
    app_factory, running_app, settings_factory, authorization: str
) -> None:
    app = app_factory(settings_override=settings_factory(api_token=TOKEN, rate_limit_requests=1))
    async with running_app(app) as client:
        codes = [
            (await client.get("/api/cameras", headers={"Authorization": authorization})).status_code
            for _ in range(2)
        ]
    assert codes == [401, 429]


async def test_the_token_does_not_exempt_the_stream_budget(
    app_factory, running_app, settings_factory
) -> None:
    """A browser's stream request can carry the token (the edge injects it). Holding many
    long-lived streams is exactly what the stream budget is for, so it still applies."""
    app = app_factory(
        settings_override=settings_factory(
            api_token=TOKEN, rate_limit_requests=100, rate_limit_stream_requests=2
        )
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with running_app(app) as client:
        streams = [
            (await client.get("/api/cameras/nope/stream.mjpg", headers=headers)).status_code
            for _ in range(3)
        ]
    assert streams == [404, 404, 429]


async def test_without_a_configured_token_nothing_is_exempt_by_credential(
    app_factory, running_app, settings_factory
) -> None:
    """No token configured: a caller cannot present a "valid" one, whatever it sends."""
    app = app_factory(settings_override=settings_factory(rate_limit_requests=1))
    async with running_app(app) as client:
        codes = [
            (
                await client.get("/api/cameras", headers={"Authorization": "Bearer anything"})
            ).status_code
            for _ in range(2)
        ]
    assert codes == [200, 429]


async def test_clients_are_limited_independently(
    app_factory, running_app, settings_factory
) -> None:
    app = app_factory(settings_override=settings_factory(rate_limit_requests=1))
    async with running_app(app) as client:
        a1 = await client.get("/api/cameras", headers={"X-Forwarded-For": "203.0.113.1"})
        a2 = await client.get("/api/cameras", headers={"X-Forwarded-For": "203.0.113.1"})
        b1 = await client.get("/api/cameras", headers={"X-Forwarded-For": "203.0.113.2"})
    assert (a1.status_code, a2.status_code, b1.status_code) == (200, 429, 200)


async def test_forwarded_for_uses_the_entry_the_trusted_proxy_appended(
    app_factory, running_app, settings_factory
) -> None:
    """REPLACES `test_forwarded_for_uses_the_leftmost_entry`. The leftmost entry is
    whatever the caller wrote, so keying on it lets anyone mint a fresh bucket per request.
    With the default one trusted proxy the client is the RIGHTMOST entry -- the address
    that proxy saw -- and everything to its left is ignored."""
    app = app_factory(settings_override=settings_factory(rate_limit_requests=1))
    async with running_app(app) as client:
        first = await client.get(
            "/api/cameras", headers={"X-Forwarded-For": "203.0.113.9, 198.51.100.7"}
        )
        # A different (spoofed) leftmost entry, same address behind the proxy: same client.
        spoofed_left = await client.get(
            "/api/cameras", headers={"X-Forwarded-For": "203.0.113.77, 198.51.100.7"}
        )
        other_client = await client.get(
            "/api/cameras", headers={"X-Forwarded-For": "203.0.113.9, 198.51.100.8"}
        )
    assert (first.status_code, spoofed_left.status_code, other_client.status_code) == (
        200,
        429,
        200,
    )


async def test_trusted_proxy_hops_counts_from_the_right(
    app_factory, running_app, settings_factory
) -> None:
    """Two proxies in front: the client is the second entry from the right."""
    app = app_factory(
        settings_override=settings_factory(rate_limit_requests=1, trusted_proxy_hops=2)
    )
    async with running_app(app) as client:
        first = await client.get(
            "/api/cameras", headers={"X-Forwarded-For": "192.0.2.1, 198.51.100.7, 10.0.0.2"}
        )
        same = await client.get(
            "/api/cameras", headers={"X-Forwarded-For": "192.0.2.99, 198.51.100.7, 10.0.0.3"}
        )
        other = await client.get(
            "/api/cameras", headers={"X-Forwarded-For": "192.0.2.1, 198.51.100.8, 10.0.0.2"}
        )
    assert (first.status_code, same.status_code, other.status_code) == (200, 429, 200)


async def test_zero_trusted_hops_ignores_forwarded_for_entirely(
    app_factory, running_app, settings_factory
) -> None:
    """A worker reachable with no proxy in front: the header is pure spoofing, so every
    request is keyed on the socket peer however it varies the header."""
    app = app_factory(
        settings_override=settings_factory(rate_limit_requests=2, trusted_proxy_hops=0)
    )
    async with running_app(app) as client:
        codes = [
            (
                await client.get("/api/cameras", headers={"X-Forwarded-For": f"203.0.113.{n}"})
            ).status_code
            for n in range(1, 4)
        ]
    assert codes == [200, 200, 429]


@pytest.mark.parametrize(
    "header",
    [
        "not-an-ip",  # the rightmost entry is not an address
        "203.0.113.1, not-an-ip",
        "198.51.100.7:8080",  # a port makes it not an address
        "203.0.113.1, ",  # blank rightmost entry
        "999.1.1.1",
        "<script>alert(1)</script>",
    ],
)
async def test_an_invalid_forwarded_for_entry_falls_back_to_the_peer(
    app_factory, running_app, settings_factory, header: str
) -> None:
    """An entry that is not an IP is never used as a key: it falls back to the socket peer,
    so these all share the one peer bucket along with a request that sent no header."""
    app = app_factory(settings_override=settings_factory(rate_limit_requests=1))
    async with running_app(app) as client:
        invalid = await client.get("/api/cameras", headers={"X-Forwarded-For": header})
        no_header = await client.get("/api/cameras")
    assert (invalid.status_code, no_header.status_code) == (200, 429)


async def test_fewer_entries_than_trusted_hops_falls_back_to_the_peer(
    app_factory, running_app, settings_factory
) -> None:
    """Two proxies are trusted but the header has one entry: the request did not come
    through the chain we expect, so the header is not believed."""
    app = app_factory(
        settings_override=settings_factory(rate_limit_requests=1, trusted_proxy_hops=2)
    )
    async with running_app(app) as client:
        short = await client.get("/api/cameras", headers={"X-Forwarded-For": "203.0.113.5"})
        no_header = await client.get("/api/cameras")
    assert (short.status_code, no_header.status_code) == (200, 429)


async def test_ipv6_spellings_of_one_address_share_a_bucket(
    app_factory, running_app, settings_factory
) -> None:
    app = app_factory(settings_override=settings_factory(rate_limit_requests=1))
    async with running_app(app) as client:
        compact = await client.get("/api/cameras", headers={"X-Forwarded-For": "2001:db8::1"})
        expanded = await client.get(
            "/api/cameras", headers={"X-Forwarded-For": "2001:0db8:0:0:0:0:0:1"}
        )
    assert (compact.status_code, expanded.status_code) == (200, 429)


async def test_header_spraying_cannot_grow_the_bucket_table_without_bound(
    app_factory, running_app, settings_factory, monkeypatch
) -> None:
    """Each distinct key is a bucket. With the cap, a caller choosing its own keys (a mis-set
    `trusted_proxy_hops`) holds at most `_MAX_BUCKETS` of them; the oldest windows go first."""
    monkeypatch.setattr(middleware_module, "_MAX_BUCKETS", 5)
    app = app_factory(settings_override=settings_factory(rate_limit_requests=100))
    async with running_app(app) as client:
        for n in range(1, 41):
            await client.get("/api/cameras", headers={"X-Forwarded-For": f"203.0.113.{n}"})

    limiter = next(m for m in _walk_stack(app) if isinstance(m, RateLimitMiddleware))
    assert len(limiter._buckets) == 5
    # The survivors are the most recent keys; the oldest were evicted first.
    assert {key[0] for key in limiter._buckets} == {f"203.0.113.{n}" for n in range(36, 41)}


async def test_without_forwarded_for_the_socket_peer_is_the_client(
    app_factory, running_app, settings_factory
) -> None:
    """No header: all of these come from the one ASGI test peer, so they share
    a bucket and a client that does send the header gets its own."""
    app = app_factory(settings_override=settings_factory(rate_limit_requests=2))
    async with running_app(app) as client:
        codes = [(await client.get("/api/cameras")).status_code for _ in range(3)]
        proxied = await client.get("/api/cameras", headers={"X-Forwarded-For": "192.0.2.50"})
    assert codes == [200, 200, 429]
    assert proxied.status_code == 200


async def test_blank_forwarded_for_falls_back_to_the_peer(
    app_factory, running_app, settings_factory
) -> None:
    # The blank entry is now the RIGHTMOST one (`"10.0.0.1, "`), the one that is read; the
    # old fixture `" , 10.0.0.1"` put it on the left, which is no longer consulted.
    app = app_factory(settings_override=settings_factory(rate_limit_requests=1))
    async with running_app(app) as client:
        first = await client.get("/api/cameras", headers={"X-Forwarded-For": "10.0.0.1, "})
        second = await client.get("/api/cameras")
    # Both fell back to the same peer address, so they share one bucket.
    assert (first.status_code, second.status_code) == (200, 429)


async def test_stream_path_has_its_own_tighter_budget(
    app_factory, running_app, settings_factory
) -> None:
    """The stream limit applies to `/stream.mjpg` and is counted separately
    from ordinary calls. An unknown camera 404s at once, so no stream is opened
    — but rate limiting runs before routing and still counts the attempt."""
    app = app_factory(
        settings_override=settings_factory(rate_limit_requests=100, rate_limit_stream_requests=2)
    )
    async with running_app(app) as client:
        streams = [
            (await client.get("/api/cameras/nope/stream.mjpg")).status_code for _ in range(3)
        ]
        ordinary = await client.get("/api/cameras")
    assert streams == [404, 404, 429]
    assert ordinary.status_code == 200  # the stream budget is separate


async def test_stream_traffic_does_not_spend_the_ordinary_budget(
    app_factory, running_app, settings_factory
) -> None:
    app = app_factory(
        settings_override=settings_factory(rate_limit_requests=2, rate_limit_stream_requests=10)
    )
    async with running_app(app) as client:
        for _ in range(5):
            await client.get("/api/cameras/nope/stream.mjpg")
        ordinary = [(await client.get("/api/cameras")).status_code for _ in range(2)]
    assert ordinary == [200, 200]


async def test_budget_resets_after_the_window(app_factory, running_app, settings_factory) -> None:
    app = app_factory(
        settings_override=settings_factory(rate_limit_requests=1, rate_limit_window_seconds=0.2)
    )
    async with running_app(app) as client:
        first = await client.get("/api/cameras")
        limited = await client.get("/api/cameras")
        await asyncio.sleep(0.3)
        after = await client.get("/api/cameras")
    assert (first.status_code, limited.status_code, after.status_code) == (200, 429, 200)


async def test_rate_limit_disabled_means_not_installed(
    app_factory, running_app, settings_factory
) -> None:
    app = app_factory(
        settings_override=settings_factory(rate_limit_enabled=False, rate_limit_requests=1)
    )
    assert RateLimitMiddleware not in [m.cls for m in app.user_middleware]
    async with running_app(app) as client:
        codes = [(await client.get("/api/cameras")).status_code for _ in range(5)]
    assert codes == [200] * 5


def _limiter(settings: Settings) -> RateLimitMiddleware:
    async def never_called(*_: object) -> None:  # pragma: no cover
        raise AssertionError("the wrapped app must not run in this test")

    return RateLimitMiddleware(never_called, settings=settings)


def test_stale_buckets_are_evicted(settings_factory) -> None:
    """Memory must not grow by one entry per address ever seen."""
    from traffic_ai.api.middleware import _Bucket

    limiter = _limiter(settings_factory(rate_limit_window_seconds=10.0))
    now = time.monotonic()
    limiter._buckets[("old-client", False)] = _Bucket(window_start=now - 100, count=5)
    limiter._buckets[("older-stream-client", True)] = _Bucket(window_start=now - 11, count=1)
    limiter._buckets[("live-client", False)] = _Bucket(window_start=now - 1, count=1)
    limiter._last_sweep = now - 11  # a full window since the last sweep

    limiter._sweep(now)

    assert set(limiter._buckets) == {("live-client", False)}


def test_sweep_runs_at_most_once_per_window(settings_factory) -> None:
    """The O(n) scan is amortised: sweeping again inside the window is a no-op."""
    from traffic_ai.api.middleware import _Bucket

    limiter = _limiter(settings_factory(rate_limit_window_seconds=10.0))
    now = time.monotonic()
    limiter._last_sweep = now - 1  # swept a second ago
    limiter._buckets[("expired", False)] = _Bucket(window_start=now - 100, count=1)

    limiter._sweep(now)

    assert ("expired", False) in limiter._buckets  # left alone until the window is up


async def test_buckets_are_evicted_through_real_traffic(
    app_factory, running_app, settings_factory
) -> None:
    """End to end: clients that go quiet are dropped on a later request."""
    app = app_factory(
        settings_override=settings_factory(rate_limit_requests=5, rate_limit_window_seconds=0.2)
    )
    async with running_app(app) as client:
        for n in range(20):
            await client.get("/api/cameras", headers={"X-Forwarded-For": f"203.0.113.{n}"})
        await asyncio.sleep(0.5)  # every bucket above is now expired
        await client.get("/api/cameras", headers={"X-Forwarded-For": "198.51.100.1"})

    limiter = next(m for m in _walk_stack(app) if isinstance(m, RateLimitMiddleware))
    assert set(limiter._buckets) == {("198.51.100.1", False)}


def _walk_stack(app: FastAPI):
    """Yield each instantiated middleware, outermost first."""
    layer = app.middleware_stack
    while layer is not None:
        yield layer
        layer = getattr(layer, "app", None)


# --- stack order ---------------------------------------------------------------


async def test_middleware_order_outermost_first(app_factory, settings_factory) -> None:
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    assert _stack_names(app) == [
        "_request_id_middleware",
        "_metrics_middleware",
        "SecurityHeadersMiddleware",
        "RateLimitMiddleware",
        "AuthMiddleware",
    ]


async def test_disabled_features_leave_no_middleware(app_factory, settings_factory) -> None:
    app = app_factory(
        settings_override=settings_factory(
            rate_limit_enabled=False, security_headers_enabled=False, metrics_enabled=False
        )
    )
    assert _stack_names(app) == ["_request_id_middleware"]


async def test_rejected_requests_still_carry_request_id_and_headers(
    app_factory, running_app, settings_factory
) -> None:
    """The point of the ordering, observed: a 401 produced by the innermost
    layer is still stamped with the request id and security headers by the
    layers outside it."""
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    async with running_app(app) as client:
        resp = await client.get("/api/cameras", headers={"X-Request-ID": "trace-me"})
    assert resp.status_code == 401
    assert resp.headers["x-request-id"] == "trace-me"
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_rate_limited_requests_carry_request_id_and_headers(
    app_factory, running_app, settings_factory
) -> None:
    app = app_factory(settings_override=settings_factory(rate_limit_requests=1))
    async with running_app(app) as client:
        await client.get("/api/cameras")
        resp = await client.get("/api/cameras", headers={"X-Request-ID": "trace-429"})
    assert resp.status_code == 429
    assert resp.headers["x-request-id"] == "trace-429"
    assert resp.headers["x-frame-options"] == "DENY"


async def test_flood_is_throttled_before_it_reaches_the_token_check(
    app_factory, running_app, settings_factory
) -> None:
    """Rate limit is outside auth: an unauthenticated flood gets 429s, not an
    endless stream of free 401s."""
    app = app_factory(settings_override=settings_factory(api_token=TOKEN, rate_limit_requests=2))
    async with running_app(app) as client:
        codes = [(await client.get("/api/cameras")).status_code for _ in range(4)]
    assert codes == [401, 401, 429, 429]


@pytest.mark.parametrize("path", ["/api/healthz", "/api/readyz"])
async def test_default_exempt_paths_match_the_settings_default(path, settings_factory) -> None:
    assert path in settings_factory().auth_exempt_paths


async def test_auth_middleware_class_is_what_the_app_installs(
    app_factory, settings_factory
) -> None:
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    assert AuthMiddleware in [m.cls for m in app.user_middleware]
