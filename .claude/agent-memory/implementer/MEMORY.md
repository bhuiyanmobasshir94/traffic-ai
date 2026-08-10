# Implementer Memory — traffic-ai

- [Ruff noqa for unselected rule codes](ruff-noqa-unused-directive.md) — BLE001/PLC0415 noqa comments fail RUF100 here; use plain comments instead.
- [supervision 0.30.0 ByteTrack quirks](supervision-bytetrack-quirks.md) — canonical import path still warns; `reset()` restarts id counter from 1.
- [Worker package structure](traffic-ai-worker-package.md) — detect/track/count/annotate pipeline, `CameraPipeline._process_frame` as the single-tick test seam.
- [Read-only anchors can change on resume](anchor-files-can-change-between-resumed-sessions.md) — re-Read anchor files after any session interruption/resume.
