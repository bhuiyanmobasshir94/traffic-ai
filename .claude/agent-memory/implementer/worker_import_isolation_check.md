---
name: worker-import-isolation-check
description: Testing "module X is never imported at module scope" fails once sibling test suites exist in the same pytest session
metadata:
  type: project
---

`traffic-ai`'s `src/traffic_ai/api/app.py` must be importable with `traffic_ai.worker`
absent (`default_pipeline_factory` imports it lazily, inside the function body — see
`api/app.py`'s `CameraPipeline` Protocol and `PipelineFactory` alias).

An in-process pytest test that pops `traffic_ai.api.app` from `sys.modules`, re-imports it,
and asserts no `traffic_ai.worker.*` module is present in `sys.modules` **works in isolation**
but is a false failure once `tests/worker/` exists alongside `tests/api/` in the same repo:
pytest's collection phase imports every `test_*.py` file (including `tests/worker/test_*.py`,
which imports `traffic_ai.worker.*` for its own fixtures) before any test body runs, so
`traffic_ai.worker.tracking` etc. are already in `sys.modules` regardless of what the api test
does.

**Fix:** check the invariant in a fresh subprocess instead —

```python
subprocess.run([sys.executable, "-c", "import traffic_ai.api.app, sys; assert not [...]"])
```

This mirrors the project's own "Done means" verification command
(`python -c "import traffic_ai.api.app, sys; assert ..."`) and is immune to what else the
pytest session has already imported. General lesson: any "package X is not imported" test
belongs in a subprocess the moment the repo might grow a sibling suite that legitimately
imports X.

See [[fastapi-fakeredis-testing]].
