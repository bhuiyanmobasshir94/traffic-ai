"""`describe_db_error` — what a database failure may say about itself in a log line."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, StatementError

from traffic_ai.db.errors import describe_db_error


class _DriverError(Exception):
    """Stands in for asyncpg's exception: its text quotes the offending row."""


def _dbapi_error(cls: type[StatementError] = DBAPIError) -> StatementError:
    return cls(
        "INSERT INTO crossing_events (plate_text) VALUES (%s)",
        ("SECRET-PLATE-0042",),
        _DriverError("DETAIL: Key (plate_text)=(SECRET-PLATE-0042) already exists."),
    )


@pytest.mark.parametrize("cls", [DBAPIError, IntegrityError, OperationalError])
def test_a_dbapi_error_is_reduced_to_its_type_and_the_drivers_class(
    cls: type[StatementError],
) -> None:
    fields = describe_db_error(_dbapi_error(cls))

    assert fields == {"error_type": cls.__name__, "error": "_DriverError"}


def test_nothing_from_the_statement_the_parameters_or_the_driver_message_survives() -> None:
    rendered = repr(describe_db_error(_dbapi_error()))

    assert "SECRET-PLATE-0042" not in rendered
    assert "crossing_events" not in rendered
    assert "DETAIL" not in rendered


def test_a_statement_error_with_no_underlying_exception_says_unknown() -> None:
    error = StatementError("bad bind", "SELECT 1", {"x": "SECRET"}, None)

    assert describe_db_error(error) == {"error_type": "StatementError", "error": "unknown"}


def test_any_other_exception_keeps_its_short_message() -> None:
    assert describe_db_error(RuntimeError("database unavailable")) == {
        "error_type": "RuntimeError",
        "error": "database unavailable",
    }


def test_a_long_message_is_truncated() -> None:
    assert len(describe_db_error(RuntimeError("x" * 5000))["error"]) == 200


@pytest.mark.parametrize(
    "message",
    [
        "Could not parse URL 'postgresql+asyncpg://traffic:hunter2@db:5432/traffic_ai'",
        "bad dsn postgresql://user:hunter2@host/db and more",
        "redis://:hunter2@cache:6379/0",
    ],
)
def test_url_credentials_in_a_message_are_masked(message: str) -> None:
    """An unparseable DSN is quoted verbatim by SQLAlchemy's `ArgumentError`, at startup."""
    error = describe_db_error(ValueError(message))["error"]

    assert "hunter2" not in error
    assert "://" in error  # the shape is kept, so the message is still diagnosable


def test_a_message_without_credentials_is_untouched() -> None:
    message = "No module named 'asyncpg'"
    assert describe_db_error(ModuleNotFoundError(message))["error"] == message
