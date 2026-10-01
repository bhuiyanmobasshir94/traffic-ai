---
name: traffic-ai-db-layer
description: Postgres persistence layer (src/traffic_ai/db, migrations/) - structure, verified SQLAlchemy/Alembic/test traps
metadata:
  type: project
---

Layout: `db/models.py` (Base, CrossingEventRow, CameraCountSnapshotRow), `db/session.py`
(`Database`), `db/repository.py` (`CrossingRepository`, `CameraCountSnapshot` DTO),
`db/writer.py` (`CrossingWriter`, takes any object with async `add_many`), `migrations/` (async env).

Verified traps:
- The venv did NOT ship sqlalchemy/asyncpg/alembic/aiosqlite. Network works, so
  `uv pip install --python .venv/bin/python "sqlalchemy[asyncio]>=2.0,<3" ...` fixes it. No `pip` module in the venv.
- Descending index in declarative: `Index(name, "camera_id", text("crossed_at DESC"))` in `__table_args__`
  compiles to `(camera_id, crossed_at DESC)`. Introspect with `index.expressions` — `index.columns` omits the text clause.
- `func.date_trunc` with bind params for 'hour' risks Postgres rejecting SELECT vs GROUP BY as different
  expressions; `literal_column("'hour'")` renders identical SQL in both places (checked by compiling).
- `pyproject` addopts already has `-q`; passing another `-q` hides the pass/fail summary line.
- `ruff check alembic.ini` parses the .ini as Python and reports errors; it is not a real lint failure
  (`migrations/versions` is in ruff `extend-exclude`, so 0001 is never linted).
- To assert structlog events: `structlog.testing.capture_logs()` plus
  `monkeypatch.setattr(module, "log", structlog.get_logger(...))` - a cached module logger can hide events
  if an earlier test configured structlog.
- `alembic -c alembic.ini history` and `upgrade head --sql` run with no database; use them to check a migration
  against the models offline.
- Files written before an API session interruption were gone on resume: `ls` the write set first.

Open/not verifiable without Postgres: every `requires_postgres` test, ON CONFLICT ON CONSTRAINT upsert,
real `Database.ping`/pool behaviour, `alembic upgrade` against a live server.
