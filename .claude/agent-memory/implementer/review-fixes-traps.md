---
name: review-fixes-traps
description: Traps hit while fixing the verified code-review findings (rate limiter, XFF, writer loss counting, schema-aware ping, shutdown event) - verified 2026-10-01
metadata:
  type: project
---

Verified in the production-hardening worktree, 2026-10-01.

- **Limiter + auth now both call `compare_digest`.** `RateLimitMiddleware` recognises a valid bearer token itself
  (exempt from the ordinary budget; the stream budget still applies), so every presented credential is compared
  twice. `tests/api/test_security.py::test_comparison_is_constant_time` pins the 4-call sequence on purpose; do not
  "optimise" by passing a flag between layers (a reordered stack would silently skip auth).
- **`Database.ping()` selects `CrossingEventRow` columns with `LIMIT 0`**, not `SELECT 1`: false for an unmigrated
  schema. Testable without Postgres via `sqlite+aiosqlite` (aiosqlite is a test dep) - `tests/db/test_session.py`.
- **`db/errors.py::describe_db_error`** is the only way DB failures are logged (`error_type` + driver class for any
  `StatementError`, truncated + URL-credential-masked text otherwise). Never `error=str(exc)` for a DB error.
- **History loss accounting:** `CrossingWriter.lost_count` = buffer evictions + failed-batch rows; `dropped_count`
  still means evictions only. Metric `history_events_lost_total{reason=buffer_full|flush_failed}` is process-wide,
  so tests assert DELTAS. Both label series are pre-created at 0 in `metrics.py`.
- **Shutdown event lives at `app.state.shutdown_event`** (set first thing in lifespan `finally`);
  `app.state.history_writer` feeds readyz `history_events_lost` (None when no writer).
- **`asyncio.wait_for` cannot prove a stream ended.** `_mjpeg_frames` swallows `CancelledError`, so on timeout
  `wait_for` cancels the consumer, which "finishes" normally, and `wait_for` returns with no `TimeoutError` - a
  stream that ignores the stop event passed my first test. Use `done, _ = await asyncio.wait({task}, timeout=2)`,
  assert `task in done`, then `task.cancel()` in `finally`. An unbounded `[x async for ...]` HANGS the suite instead.
  Verified by mutation (`while True:` in place of the stop check): all four stream tests now fail in ~8s.
- **Mutation scripts must not be killed with SIGTERM** (`finally` does not run); kill the pytest child instead.
  The script at /tmp/mutate_review_fixes.py pipes through `tail`, so nothing prints until it ends - run it in the
  background with `run_in_background` rather than the 580s foreground limit (27 mutants x ~20s).
- **Ruff RUF001** rejects literal NO-BREAK/zero-width spaces in test strings; build them with `chr(0xA0)`.
- **httpx refuses a non-ASCII `str` header** value (UnicodeEncodeError); send `bytes` (latin-1) to model what
  Starlette receives.
- Sibling worker's `tests/test_deployment_config.py` / `test_helm_chart.py` assert on names from this slice:
  `history_events_lost_total`, `ReadinessResponse.history_events_lost`, `Settings.trusted_proxy_hops`.
