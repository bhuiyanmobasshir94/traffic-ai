---
name: api-middleware-stack-traps
description: Verified traps when wiring Starlette/FastAPI middleware, auth, and Prometheus in traffic_ai.api (ordering, compare_digest, httpx headers, metrics label)
metadata:
  type: project
---

Verified against starlette 1.7.0 / fastapi 0.142 / prometheus-client 0.26 in this repo's venv.

- **Registration order is the reverse of nesting.** `app.middleware("http")` and `add_middleware`
  both `insert(0, ...)`, so the LAST registered is OUTERMOST. `create_app` registers
  auth, rate limit, security headers, metrics, request-id in that order to get
  request-id -> metrics -> headers -> rate limit -> auth -> routes. The pre-existing
  single `app.middleware("http")(_request_id_middleware)` line looked fine alone but would have
  become the INNERMOST layer had the new ones been added after it.
- **`secrets.compare_digest` raises TypeError on a non-ASCII `str`.** Starlette decodes header
  bytes as latin-1, so `Authorization: Bearer <non-ascii>` is deliverable and would 500. Compare
  as UTF-8 bytes (`security.tokens_match`). Mutation-checked: tests fail without the encode.
- **httpx refuses a non-ASCII `str` header value** (ascii-encodes it). To send a hostile header
  in a test, pass `header.encode("latin-1")` bytes.
- **Route template for metrics labels:** `request.scope["route"].path` is populated by the router
  in place on the shared scope dict, so it is readable after `call_next` returns. Anything
  rejected before routing (401/429) has no route -> label it `"unmatched"`, never the raw path.
- **Metrics registry is module-global**, so counters accumulate across tests in a session. Assert
  deltas, not absolutes. Unhandled-exception tests need `pytest.raises` because the
  `ASGITransport` re-raises app exceptions.
- **Do not hit `/stream.mjpg` for a real camera in a test** - it streams until the TTL stalls.
  An unknown camera id 404s instantly and still exercises the rate limiter (it runs before routing).
- `prometheus-client` is declared in pyproject extras but was NOT installed in the venv; install
  with `uv pip install --python .venv/bin/python "prometheus-client>=0.21,<1.0"`.
- Worktree sandbox refuses compound shell with `$(...)` substitution; use plain separate commands.
