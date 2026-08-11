---
name: fastapi-fakeredis-testing
description: How to test a FastAPI app whose lifespan builds/uses an async fakeredis-backed store, without cross-event-loop flakiness
metadata:
  type: project
---

In `traffic-ai`, `tests/conftest.py`'s `store` fixture is an async pytest fixture wrapping
`fakeredis.aioredis.FakeRedis`. Its internal asyncio primitives bind to whichever event loop
first touches them.

Starlette's `TestClient` (`with TestClient(app) as client:`) drives requests from a
background-thread event loop that is **not** the loop pytest-asyncio runs the test coroutine
(and the `store` fixture's setup/teardown) on. Verified in this repo: combining `TestClient`
with the async `store`/`settings` fixtures from `tests/conftest.py` risks "attached to a
different loop" failures on fixture teardown (`await s.close()` running on a different loop
than the one that created the client's internals).

**Fix used in `tests/api/`:** never use `TestClient`. Instead run the app's lifespan and issue
requests on the *same* loop as the test coroutine:

```python
async with app.router.lifespan_context(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        ...
```

This is wrapped as the `running_app` fixture in `tests/api/conftest.py` (exposed as a fixture,
not a plain importable function — `tests/api/` has no `__init__.py`, so dotted imports like
`from tests.api.conftest import running_app` are not reliable under pytest's default
"prepend" import mode; fixtures are auto-discovered regardless).

**Caveat found the same session:** `httpx.ASGITransport` does not reliably propagate an early
client-side stream close as `asyncio.CancelledError` into the server-side generator in useful
time — a real MJPEG-style `StreamingResponse` test that breaks out of `client.stream()` early
still took the full `Settings.state_ttl_seconds` (its cap-the-stream fallback) to actually
finish, not an immediate cancellation. Don't rely on ASGITransport for disconnect-timing
assertions; test the streaming generator function directly (call it, iterate it, cancel the
consuming task) for that behavior instead — see `tests/api/test_stream.py`.

See [[worker-import-isolation-check]].
