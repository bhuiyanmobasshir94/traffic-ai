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


class TestTokenWhitespaceAndControlCharacters:
    """A token with a trailing newline (the usual secret-file accident) cannot be sent as a
    header value, so every client fails to authenticate -- silently, from the operator's seat.
    Refused at startup, in every environment, by a message that never quotes the token."""

    @pytest.mark.parametrize(
        "token",
        [
            "a" * 40 + "\n",
            "a" * 40 + "\r\n",
            " " + "a" * 40,
            "a" * 20 + " " + "a" * 20,
            "a" * 20 + "\t" + "a" * 20,
            "a" * 40 + "\x00",
            "a" * 40 + "\x1b",
            "a" * 40 + chr(0xA0),  # no-break space
            "a" * 40 + chr(0x200B),  # zero-width space: not whitespace, not printable
            # Printable but non-ASCII: ui/client.py refuses to send it, so the worker must
            # refuse to start with it rather than accept a token no client can present.
            "a" * 40 + "é",
        ],
    )
    @pytest.mark.parametrize("environment", ["development", "production"])
    def test_a_token_containing_whitespace_or_a_control_character_is_refused(
        self, token: str, environment: str
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            build(environment=environment, api_token=token, database_url=REAL_DB_URL)

        message = problems_from(caught.value)
        assert "TRAFFIC_AI_API_TOKEN must be visible ASCII" in message
        assert "a" * 40 not in message  # the value is never echoed

    def test_a_clean_token_with_punctuation_is_accepted(self) -> None:
        token = "Zx9-_.~+/=" + "k" * 30
        assert build(api_token=token).api_token.get_secret_value() == token  # type: ignore[union-attr]

    def test_an_all_whitespace_token_is_still_just_absent(self) -> None:
        """Blank is the compose `${VAR:-}` case, handled before this check: not an error."""
        assert build(api_token="\n").api_token is None  # noqa: S106 - a test value


class TestErrorsDoNotQuoteSecrets:
    """The production gate raises from a model-level validator, whose "input" is the whole
    settings dict. By default pydantic prints it in the error, token and database URL included,
    straight into the container log of a service that has just refused to start."""

    SECRET_TOKEN = "SENTINEL-token-" + "q" * 30
    SECRET_URL = "postgresql+asyncpg://traffic:SENTINEL-db-password@db:5432/traffic_ai"  # noqa: S105

    def test_the_production_gate_error_names_the_problem_but_not_the_token(self) -> None:
        with pytest.raises(ValidationError) as caught:
            # Dev password in the URL trips the gate, with a real-looking token alongside.
            build(
                environment="production",
                api_token=self.SECRET_TOKEN,
                database_url="postgresql+asyncpg://traffic:traffic@db:5432/traffic_ai",
            )

        message = problems_from(caught.value)
        assert "development password" in message  # it is the gate speaking
        assert self.SECRET_TOKEN not in message
        assert "input_value" not in message

    def test_the_gate_error_does_not_quote_the_database_url_either(self) -> None:
        with pytest.raises(ValidationError) as caught:
            build(
                environment="production",
                api_token="short",  # noqa: S106 - a deliberately weak test value
                database_url=self.SECRET_URL,
            )

        message = problems_from(caught.value)
        assert f"shorter than {MIN_API_TOKEN_LENGTH}" in message
        assert "SENTINEL-db-password" not in message

    def test_a_field_validation_error_does_not_quote_its_input_either(self) -> None:
        with pytest.raises(ValidationError) as caught:
            build(state_ttl_seconds="SENTINEL-not-a-number")

        assert "SENTINEL-not-a-number" not in problems_from(caught.value)


class TestTrustedProxyHops:
    def test_defaults_to_one_proxy_the_compose_stack(self) -> None:
        assert build().trusted_proxy_hops == 1

    def test_zero_is_allowed_for_a_worker_with_no_proxy_in_front(self) -> None:
        assert build(trusted_proxy_hops=0).trusted_proxy_hops == 0

    def test_a_negative_count_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            build(trusted_proxy_hops=-1)

    def test_is_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRAFFIC_AI_TRUSTED_PROXY_HOPS", "2")
        assert build().trusted_proxy_hops == 2
