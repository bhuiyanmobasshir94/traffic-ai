# Implementer Memory — traffic-ai

- [Ruff noqa for unselected rule codes](ruff-noqa-unused-directive.md) — BLE001/PLC0415 noqa comments fail RUF100 here; use plain comments instead.
- [supervision 0.30.0 ByteTrack quirks](supervision-bytetrack-quirks.md) — canonical import path still warns; `reset()` restarts id counter from 1.
- [Worker package structure](traffic-ai-worker-package.md) — detect/track/count/annotate pipeline, `CameraPipeline._process_frame` as the single-tick test seam.
- [Postgres persistence layer](traffic-ai-db-layer.md) — db/ package layout, missing-venv-deps fix, SQLAlchemy/alembic/test traps, what needs a live PG.
- [API middleware stack traps](api-middleware-stack-traps.md) — reverse-registration order, compare_digest non-ASCII TypeError, httpx header bytes, metrics route label.
- [Helm chart notes](helm-chart-notes.md) — chart decisions, `--dry-run=client` shows NOTES w/o cluster, TORCH_HOME/readOnlyRootFilesystem, test lint traps.
- [Read-only anchors can change on resume](anchor-files-can-change-between-resumed-sessions.md) — re-Read anchor files after any session interruption/resume.
