---
name: history-routes-and-persistence-wiring
description: How Postgres history/metrics are wired into the pipeline, lifespan and /api/history routes - design traps and test seams verified 2026-10-01
metadata:
  type: project
---

Layout: `db/sink.py` (`RepositorySink` write side for `CrossingWriter`, `RepositoryHistory` read side for
routes), `api/dependencies.py` (`get_history_camera_id`, `get_history_window`, `get_history_reader`,
`HistoryReader` Protocol), `api/routes.py` (`/history/{events,counts,hourly}`, `_probe_database`),
`api/app.py` lifespan (writer task, shutdown order, `app.state.database` / `app.state.history`).

Verified traps:
- **FastAPI runs ALL dependencies before validating the endpoint's own params.** A dependency that
  raised 503 would shadow a 422 for `limit=0`. So `get_history_reader` returns `_UnavailableHistory`
  (raises 503 on first query use) instead of raising. Camera/window deps raise 404/422 first.
- **Shutdown order is load-bearing:** pipelines stop -> `writer.request_stop()` + await drain ->
  `database.close()` (only if the app built it) -> store close. Proven by mutation: tests fail if the
  writer is stopped first or the DB is closed before the drain. A winding-down pipeline can still
  `submit()` after `request_stop()`.
- `PipelineFactory` is now `Callable[..., list[CameraPipeline]]`; `_build_pipelines` passes `writer=`
  only to factories that declare a `writer` param (inspect.signature), so 2-arg factories still work.
- `Settings.persistence_enabled` defaults True: any test building the app would create a lazy engine at
  `postgres:5432` and `/readyz` would probe it. `tests/api/conftest.py::app_factory` therefore forces it
  off unless `persistence=True` or `database=`/`writer=` is passed.
- Pipeline feeds writer + `crossings_total` BEFORE `store.append_events`, and sets tick metrics BEFORE
  `publish_state`, so a Redis fault cannot cost history or blind the scrape. Metric failures warn ONCE
  per pipeline (`_metrics_failure_logged`), not per tick.
- `CrossingWriter.run()` swallows sink exceptions - asserting inside a fake sink is useless; record to a
  list and assert in the test body.
- Ruff here: B008 forbids `= Query()` defaults (use `Annotated[..., Query()] = None`); UP047 wants PEP 695
  generics (`async def f[T](...)`), py312 target.
- Worktree shell refuses `cat >> file <<EOF` heredocs; append to a test file with the Edit tool instead.
- Mutation-check script pattern (apply one edit, run named test, always restore from a /tmp backup)
  found one toothless test on the first pass - worth repeating for ordering/failure-isolation tests.

Not verifiable without Postgres: `RepositoryHistory`/`RepositorySink` against a real engine
(`tests/db/test_sink.py::test_sink_writes_are_visible_to_history_reads`, requires_postgres).
