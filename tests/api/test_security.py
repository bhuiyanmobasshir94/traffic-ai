"""Bearer-token authentication: the pure helpers, then the app end to end."""

from __future__ import annotations

import secrets

import pytest

from traffic_ai.api import middleware, security
from traffic_ai.api.middleware import AuthMiddleware
from traffic_ai.domain import PipelineStatus

TOKEN = "correct-horse-battery-staple-0123456789"  # noqa: S105 - a test fixture, not a credential
EXEMPT = ("/api/healthz", "/api/readyz")


# --- pure helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (f"Bearer {TOKEN}", TOKEN),
        (f"bearer {TOKEN}", TOKEN),  # scheme is case-insensitive (RFC 7235)
        (f"BEARER {TOKEN}", TOKEN),
        (f"Bearer    {TOKEN}  ", TOKEN),  # stray whitespace around the credential
        (None, None),
        ("", None),
        ("Bearer", None),  # scheme, no credential
        ("Bearer ", None),  # scheme, empty credential
        ("Bearer    ", None),
        (f"Basic {TOKEN}", None),  # wrong scheme
        (f"Token {TOKEN}", None),
        (TOKEN, None),  # a bare token with no scheme
        (f"Bearer{TOKEN}", None),  # no separator
        ("\x00\x01\x02", None),
    ],
)
def test_extract_bearer_token(header: str | None, expected: str | None) -> None:
    assert security.extract_bearer_token(header) == expected


def test_tokens_match_accepts_only_the_exact_token() -> None:
    assert security.tokens_match(TOKEN, TOKEN) is True
    assert security.tokens_match(TOKEN[:-1], TOKEN) is False  # prefix
    assert security.tokens_match(TOKEN + "x", TOKEN) is False  # extension
    assert security.tokens_match(TOKEN.upper(), TOKEN) is False
    assert security.tokens_match("", TOKEN) is False


@pytest.mark.parametrize("provided", ["é", "token-with-ünïcode", "ÿþ", "\ud800"])
def test_tokens_match_never_raises_on_non_ascii(provided: str) -> None:
    """`secrets.compare_digest` raises TypeError for a non-ASCII `str`. A header
    value is attacker-controlled, so that would turn a 401 into a 500."""
    assert security.tokens_match(provided, TOKEN) is False


def test_is_path_exempt_is_exact_membership_not_prefix() -> None:
    assert security.is_path_exempt("/api/healthz", EXEMPT) is True
    assert security.is_path_exempt("/api/readyz", EXEMPT) is True
    assert security.is_path_exempt("/api/healthz/extra", EXEMPT) is False
    assert security.is_path_exempt("/api/healthzz", EXEMPT) is False
    assert security.is_path_exempt("/api/cameras", EXEMPT) is False
    assert security.is_path_exempt("/api/metrics", EXEMPT) is False


def _authorized(path: str, header: str | None) -> bool:
    return security.is_authorized(
        path=path, authorization_header=header, api_token=TOKEN, exempt_paths=EXEMPT
    )


def test_is_authorized_exempt_path_needs_no_header() -> None:
    assert _authorized("/api/healthz", None) is True
    assert _authorized("/api/readyz", "Bearer wrong") is True


def test_is_authorized_requires_a_valid_token_elsewhere() -> None:
    assert _authorized("/api/cameras", f"Bearer {TOKEN}") is True
    assert _authorized("/api/cameras", None) is False
    assert _authorized("/api/cameras", "Bearer wrong") is False
    assert _authorized("/api/cameras", "Basic abc") is False
    assert _authorized("/api/cameras", "Bearer") is False
    assert _authorized("/api/cameras", "é") is False


# --- the app, end to end ---------------------------------------------------


async def test_auth_is_off_by_default(app_factory, running_app) -> None:
    """No `api_token` configured: every endpoint answers with no credentials.
    This is the backward-compatibility guarantee for every pre-existing test."""
    app = app_factory()
    async with running_app(app) as client:
        resp = await client.get("/api/cameras")
    assert resp.status_code == 200
    assert "www-authenticate" not in resp.headers


async def test_auth_middleware_not_installed_when_no_token(app_factory) -> None:
    app = app_factory()
    assert AuthMiddleware not in [m.cls for m in app.user_middleware]


async def test_missing_token_is_401_with_challenge(
    app_factory, running_app, settings_factory
) -> None:
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    async with running_app(app) as client:
        resp = await client.get("/api/cameras")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize(
    "header",
    [
        "Bearer wrong-token",
        f"Bearer {TOKEN}x",
        f"Bearer {TOKEN[:-1]}",
        f"Basic {TOKEN}",
        "Bearer",
        "Bearer ",
        TOKEN,
        "garbage",
        "Bearer é",  # non-ASCII must be a 401, not a TypeError-driven 500
        "Bearer ÿþý",
    ],
)
async def test_bad_credentials_are_401_not_500(
    app_factory, running_app, settings_factory, header
) -> None:
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    async with running_app(app) as client:
        # Sent as raw latin-1 bytes: httpx refuses to encode a non-ASCII `str`
        # header, but a hostile client is not bound by httpx's politeness.
        resp = await client.get("/api/cameras", headers={"Authorization": header.encode("latin-1")})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_correct_token_is_accepted(app_factory, running_app, settings_factory) -> None:
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    async with running_app(app) as client:
        resp = await client.get("/api/cameras", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_401_never_echoes_the_token(app_factory, running_app, settings_factory) -> None:
    """Neither the real token nor the caller's attempt may appear in an error."""
    attempt = "attacker-guess-should-not-be-reflected"
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    async with running_app(app) as client:
        resp = await client.get("/api/cameras", headers={"Authorization": f"Bearer {attempt}"})
    assert resp.status_code == 401
    everything = resp.text + str(dict(resp.headers))
    assert TOKEN not in everything
    assert attempt not in everything


async def test_rejection_is_logged_without_the_credential(
    app_factory, running_app, settings_factory, monkeypatch
) -> None:
    """The log line records that a request was refused, never what it carried."""
    events: list[tuple[str, dict[str, object]]] = []

    class _Recorder:
        def warning(self, event: str, **kwargs: object) -> None:
            events.append((event, kwargs))

        def __getattr__(self, name: str):
            return lambda *a, **k: events.append((name, k))

    monkeypatch.setattr(middleware, "logger", _Recorder())
    attempt = "attacker-guess-should-not-be-logged"
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    async with running_app(app) as client:
        await client.get("/api/cameras", headers={"Authorization": f"Bearer {attempt}"})

    rejected = [kwargs for event, kwargs in events if event == "auth.rejected"]
    assert len(rejected) == 1
    rendered = repr(events)
    assert attempt not in rendered
    assert TOKEN not in rendered
    assert "Bearer" not in rendered
    assert rejected[0]["credential_supplied"] is True


async def test_health_and_readiness_stay_open_with_a_token_set(
    app_factory, running_app, settings_factory, make_pipeline
) -> None:
    """A load balancer cannot present credentials; a probe that 401s looks like
    an outage."""
    app = app_factory(
        pipelines=[make_pipeline("toll-plaza-a", PipelineStatus.RUNNING)],
        settings_override=settings_factory(api_token=TOKEN),
    )
    async with running_app(app) as client:
        health = await client.get("/api/healthz")
        ready = await client.get("/api/readyz")
    assert health.status_code == 200
    assert ready.status_code == 200
    assert ready.json()["ready"] is True


async def test_readiness_503_is_not_mistaken_for_an_auth_failure(
    app_factory, running_app, settings_factory
) -> None:
    """An unready instance answers 503 to an anonymous probe, never 401."""
    app = app_factory(pipelines=[], settings_override=settings_factory(api_token=TOKEN))
    async with running_app(app) as client:
        resp = await client.get("/api/readyz")
    assert resp.status_code == 503
    assert "www-authenticate" not in resp.headers


async def test_exempt_paths_are_read_from_settings(
    app_factory, running_app, settings_factory
) -> None:
    app = app_factory(
        settings_override=settings_factory(
            api_token=TOKEN, auth_exempt_paths=("/api/healthz", "/api/cameras")
        )
    )
    async with running_app(app) as client:
        open_path = await client.get("/api/cameras")
        closed_path = await client.get("/api/readyz")
    assert open_path.status_code == 200
    assert closed_path.status_code == 401


async def test_stream_path_is_covered_by_auth(app_factory, running_app, settings_factory) -> None:
    """Middleware, not per-route dependencies, so the MJPEG stream is covered
    too. An unknown camera 404s immediately, so the test never opens a stream."""
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    async with running_app(app) as client:
        anonymous = await client.get("/api/cameras/nope/stream.mjpg")
        authenticated = await client.get(
            "/api/cameras/nope/stream.mjpg", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert anonymous.status_code == 401
    assert authenticated.status_code == 404  # past auth, rejected by the camera allowlist


async def test_comparison_is_constant_time(
    app_factory, running_app, settings_factory, monkeypatch
) -> None:
    """The check goes through `secrets.compare_digest`, for right and wrong
    tokens alike, and never short-circuits on a plain `==`."""
    real = secrets.compare_digest
    calls: list[tuple[bytes, bytes, bool]] = []

    def spy(a: bytes, b: bytes) -> bool:
        result = real(a, b)
        calls.append((a, b, result))
        return result

    monkeypatch.setattr(security.secrets, "compare_digest", spy)
    app = app_factory(settings_override=settings_factory(api_token=TOKEN))
    async with running_app(app) as client:
        await client.get("/api/cameras", headers={"Authorization": f"Bearer {TOKEN}"})
        await client.get("/api/cameras", headers={"Authorization": "Bearer nope"})
        # A request with no credential has nothing to compare, and must not.
        await client.get("/api/cameras")

    # Every presented credential is compared twice: once by the rate limiter, to recognise
    # a trusted caller and not throttle it, and once by `AuthMiddleware`, which makes the
    # real decision and deliberately trusts nothing the limiter concluded (a flag passed
    # between layers is a decision a reordered stack would silently skip). The point of
    # this test stands: both are `compare_digest`, and a credential-less request has
    # nothing to compare.
    assert [(a, b, ok) for a, b, ok in calls] == [
        (TOKEN.encode(), TOKEN.encode(), True),  # rate limiter
        (TOKEN.encode(), TOKEN.encode(), True),  # AuthMiddleware
        (b"nope", TOKEN.encode(), False),  # rate limiter
        (b"nope", TOKEN.encode(), False),  # AuthMiddleware
    ]


def test_security_module_has_no_equality_comparison_on_tokens() -> None:
    """Belt and braces for the spy above: the credential comparison is
    `compare_digest` and nothing in the module compares with `==` or `!=`. Read
    from the syntax tree because an `==` is a timing oracle that no behavioural
    test can see."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(security))
    equality_ops = (ast.Eq, ast.NotEq)
    comparisons = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare) and any(isinstance(op, equality_ops) for op in node.ops)
    ]
    # `scheme.lower() != "bearer"` compares a scheme NAME, which is public; the
    # only such comparison permitted is the one in `extract_bearer_token`.
    offenders = [n for n in comparisons if "_BEARER_SCHEME" not in ast.unparse(n)]
    assert offenders == [], [ast.unparse(n) for n in offenders]
    assert "compare_digest" in inspect.getsource(security.tokens_match)


def test_auth_middleware_refuses_to_exist_without_a_token(settings_factory) -> None:
    """Constructing an auth layer with nothing to check against would silently
    let everyone in. It must fail loudly instead."""
    with pytest.raises(ValueError, match="api_token"):
        AuthMiddleware(lambda *_: None, settings=settings_factory(api_token=None))
