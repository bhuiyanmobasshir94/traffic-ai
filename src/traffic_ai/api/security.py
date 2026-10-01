"""Bearer-token verification.

This module is deliberately framework-free: every function here takes and
returns plain strings and booleans, never a `Request`, `Response`, or
`Settings` object. That is what makes it directly unit-testable without
spinning up an ASGI app, and it is what keeps the one security-critical
comparison — "does the caller's token match ours" — in one small, easy to
audit place instead of smeared across a middleware's control flow.

`traffic_ai.api.middleware.AuthMiddleware` is the only caller. It owns the
`Settings` lookup (whether auth is enabled, what the real token is, which
paths are exempt) and passes the resulting plain values in here.

Security rules enforced here, and why:

- Token comparison uses `secrets.compare_digest`, never `==`. A plain `==`
  on strings short-circuits at the first mismatched byte, so a timing attack
  can recover the token one byte at a time by measuring response latency.
  That is a real, documented class of attack on bearer-token APIs, not a
  theoretical nit.
- No function here ever logs, formats, or re-raises the token value. A
  caller that wants to log an auth failure logs the fact of failure, never
  the credential that failed.
- Every parser here fails closed on malformed input by returning `None` or
  `False` — never raises. An attacker who can crash the auth check with a
  weird header has turned a 401 into a 500, which is a worse information
  leak (a stack trace) for no benefit to anyone legitimate.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence

_BEARER_SCHEME = "bearer"


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Pull the credential out of an `Authorization: Bearer <token>` header.

    Returns `None` for anything that is not exactly that shape: a missing
    header, a different scheme (`Basic`, `Digest`, ...), a scheme with no
    credential, or a credential that is empty after stripping whitespace.
    Never raises — a malformed header is just an absent token, not an error.
    """
    if not authorization_header:
        return None
    scheme, separator, credential = authorization_header.partition(" ")
    if not separator or scheme.lower() != _BEARER_SCHEME:
        return None
    credential = credential.strip()
    return credential or None


def tokens_match(provided: str, expected: str) -> bool:
    """Constant-time comparison of a caller-supplied token against the real one.

    Always `secrets.compare_digest`, never `==` — see the module docstring.

    Compared as UTF-8 bytes, not as `str`: `compare_digest` raises `TypeError`
    when handed a `str` containing a non-ASCII character, and a header value
    is attacker-controlled (Starlette decodes it as latin-1, so `Bearer é` is a
    perfectly deliverable request). Left as `str`, that request would be a 500
    instead of a 401. `surrogatepass` keeps the encode total for the same
    reason: this function must have no input that makes it raise.
    """
    return secrets.compare_digest(
        provided.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass")
    )


def is_path_exempt(path: str, exempt_paths: Sequence[str]) -> bool:
    """True when `path` is one of the auth-exempt paths (health/readiness).

    Exact membership, not a prefix match: the exempt set is a short, fixed
    list of probe endpoints, and a prefix match would risk accidentally
    exempting something under it later.
    """
    return path in exempt_paths


def is_authorized(
    *,
    path: str,
    authorization_header: str | None,
    api_token: str,
    exempt_paths: Sequence[str],
) -> bool:
    """True when the request may proceed.

    Composes the three checks above: an exempt path is always authorized;
    anything else needs a well-formed `Bearer` header whose credential
    matches `api_token` in constant time. Callers that have auth disabled
    entirely (no token configured) short-circuit before ever calling this —
    there is no "no token configured" case to represent here.
    """
    if is_path_exempt(path, exempt_paths):
        return True
    provided = extract_bearer_token(authorization_header)
    if provided is None:
        return False
    return tokens_match(provided, api_token)
