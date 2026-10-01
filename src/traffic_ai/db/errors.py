"""What a database failure may say about itself in a log line.

`str()` of a SQLAlchemy `DBAPIError` is the failing SQL statement, the bound
parameters, and the driver's own message. The parameters are crossing rows (and,
once a plate model exists, plate text -- personal data), and the driver message can
quote row values too: Postgres appends `DETAIL: Key (...)=(...) already exists`.
A log line is the wrong place for any of that, so database errors are logged through
`describe_db_error` rather than `error=str(exc)`.
"""

from __future__ import annotations

import re

from sqlalchemy.exc import StatementError

# Long enough for "No module named 'asyncpg'" or a pool timeout; short enough that a
# message that does carry something it should not cannot carry much of it.
_MAX_MESSAGE_CHARS = 200

# `scheme://user:password@host` -- an unparseable DSN is quoted verbatim by
# SQLAlchemy's `ArgumentError`, credentials and all, and startup failures are exactly
# where that error appears.
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")


def describe_db_error(exc: BaseException) -> dict[str, str]:
    """Structured log fields for a database failure: the type, and a safe short message.

    For a `StatementError` (which covers every `DBAPIError`) the message is only the
    class of the underlying driver exception -- never its text, never the statement,
    never the parameters. For anything else it is `str(exc)`, truncated, with any
    `scheme://user:password@` credentials masked.
    """
    fields = {"error_type": type(exc).__name__}
    if isinstance(exc, StatementError):
        fields["error"] = type(exc.orig).__name__ if exc.orig is not None else "unknown"
    else:
        message = _URL_CREDENTIALS.sub(r"\g<scheme>***@", str(exc))
        fields["error"] = message[:_MAX_MESSAGE_CHARS]
    return fields
