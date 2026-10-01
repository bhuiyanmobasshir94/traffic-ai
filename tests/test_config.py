"""`Settings` — the production hardening gate.

These tests exist because the failure they guard against is silent. A production
deployment that comes up unauthenticated, or still pointed at the development
database password, looks completely healthy: it serves 200s, the dashboard
renders, and the healthcheck passes. Nothing surfaces the problem until someone
finds the open endpoint. So the check lives at construction time and these tests
pin it there.

Every Settings here is built with `_env_file=None` so a developer's real `.env`
cannot change the outcome of the run — the same reason `tests/conftest.py` does it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from traffic_ai.config import DEV_DATABASE_PASSWORD, MIN_API_TOKEN_LENGTH, Settings

# Long enough to clear MIN_API_TOKEN_LENGTH; the value itself is irrelevant.
GOOD_TOKEN = "a" * 64
REAL_DB_URL = "postgresql+asyncpg://traffic:a-real-secret@db:5432/traffic_ai"


def build(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def problems_from(exc: ValidationError) -> str:
    """Flatten a ValidationError to the message text, for substring assertions."""
    return str(exc)


class TestBlankTokenCoercion:
    """compose.yaml passes `${TRAFFIC_AI_API_TOKEN:-}`, so "unset" arrives as "".

    Without coercion pydantic builds `SecretStr("")`, `auth_enabled` reports True,
    and a constant-time compare against an empty bearer succeeds. That is worse
    than no auth at all, because it looks authenticated.
    """

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
    def test_blank_token_becomes_none(self, blank: str) -> None:
        settings = build(api_token=blank)
        assert settings.api_token is None
        assert settings.auth_enabled is False

    def test_a_real_token_survives(self) -> None:
        settings = build(api_token=GOOD_TOKEN)
        assert settings.api_token is not None
        assert settings.api_token.get_secret_value() == GOOD_TOKEN
        assert settings.auth_enabled is True

    def test_token_is_not_exposed_by_repr(self) -> None:
        """A Settings dump reaches logs and crash reports; the token must not."""
        settings = build(api_token=GOOD_TOKEN)
        assert GOOD_TOKEN not in repr(settings)
        assert GOOD_TOKEN not in str(settings)


class TestDevelopmentStaysPermissive:
    """The gate must never get in the way of running locally."""

    def test_defaults_build(self) -> None:
        settings = build()
        assert settings.environment == "development"
        assert settings.auth_enabled is False
        assert settings.is_production is False

    def test_development_allows_no_token_and_the_dev_database(self) -> None:
        settings = build(environment="development")
        assert DEV_DATABASE_PASSWORD in settings.database_url
        assert settings.auth_enabled is False


class TestProductionFailsClosed:
    def test_production_without_a_token_is_refused(self) -> None:
        with pytest.raises(ValidationError) as caught:
            build(environment="production", database_url=REAL_DB_URL)
        assert "TRAFFIC_AI_API_TOKEN is unset" in problems_from(caught.value)

    def test_production_with_a_blank_token_is_refused(self) -> None:
        """The coercion above must feed the gate, not bypass it."""
        with pytest.raises(ValidationError) as caught:
            build(environment="production", api_token="", database_url=REAL_DB_URL)
        assert "TRAFFIC_AI_API_TOKEN is unset" in problems_from(caught.value)

    def test_production_with_a_short_token_is_refused(self) -> None:
        with pytest.raises(ValidationError) as caught:
            build(
                environment="production",
                api_token="tooshort",  # noqa: S106 - a deliberately weak test value
                database_url=REAL_DB_URL,
            )
        assert f"shorter than {MIN_API_TOKEN_LENGTH}" in problems_from(caught.value)

    def test_token_at_exactly_the_minimum_is_accepted(self) -> None:
        settings = build(
            environment="production",
            api_token="x" * MIN_API_TOKEN_LENGTH,
            database_url=REAL_DB_URL,
        )
        assert settings.auth_enabled is True

    def test_production_still_carrying_the_dev_database_password_is_refused(self) -> None:
        with pytest.raises(ValidationError) as caught:
            build(environment="production", api_token=GOOD_TOKEN)
        assert "development password" in problems_from(caught.value)

    def test_the_dev_database_password_is_tolerated_when_persistence_is_off(self) -> None:
        """Nothing connects, so the credential is inert — do not block on it."""
        settings = build(
            environment="production",
            api_token=GOOD_TOKEN,
            persistence_enabled=False,
        )
        assert settings.persistence_enabled is False

    def test_both_problems_are_reported_together(self) -> None:
        """An operator should learn everything that is wrong in one startup attempt,
        not fix one thing, redeploy, and discover the next."""
        with pytest.raises(ValidationError) as caught:
            build(environment="production")
        message = problems_from(caught.value)
        assert "TRAFFIC_AI_API_TOKEN is unset" in message
        assert "development password" in message

    def test_a_correctly_configured_production_builds(self) -> None:
        settings = build(
            environment="production",
            api_token=GOOD_TOKEN,
            database_url=REAL_DB_URL,
        )
        assert settings.is_production is True
        assert settings.auth_enabled is True

    def test_running_open_requires_saying_so_explicitly(self) -> None:
        """The escape hatch exists, but it has to be chosen by name."""
        settings = build(
            environment="production",
            allow_unauthenticated=True,
            database_url=REAL_DB_URL,
        )
        assert settings.auth_enabled is False
        assert settings.allow_unauthenticated is True


class TestHardeningDefaults:
    """Defaults must be the safe ones — an operator who sets nothing gets protection."""

    def test_security_middleware_is_on_by_default(self) -> None:
        settings = build()
        assert settings.security_headers_enabled is True
        assert settings.rate_limit_enabled is True
        assert settings.metrics_enabled is True

    def test_health_endpoints_are_auth_exempt(self) -> None:
        """A load balancer cannot present a credential, and a probe that 401s
        reads as an outage."""
        settings = build()
        assert "/api/healthz" in settings.auth_exempt_paths
        assert "/api/readyz" in settings.auth_exempt_paths

    def test_the_stream_is_rate_limited_more_tightly_than_json(self) -> None:
        settings = build()
        assert settings.rate_limit_stream_requests < settings.rate_limit_requests

    def test_metrics_path_sits_under_the_api_prefix(self) -> None:
        """So the existing Traefik /api router reaches it and the token covers it."""
        assert build().metrics_path.startswith("/api")
