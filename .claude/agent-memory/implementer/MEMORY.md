# Implementer Memory — traffic-ai

- [Ruff noqa for unselected rule codes](ruff-noqa-unused-directive.md) — BLE001/PLC0415 noqa comments fail RUF100 here; use plain comments instead.
- [supervision 0.30.0 ByteTrack quirks](supervision-bytetrack-quirks.md) — canonical import path still warns; `reset()` restarts id counter from 1.
- [Worker package structure](traffic-ai-worker-package.md) — detect/track/count/annotate pipeline, `CameraPipeline._process_frame` as the single-tick test seam.
- [Read-only anchors can change on resume](anchor-files-can-change-between-resumed-sessions.md) — re-Read anchor files after any session interruption/resume.
- [uv lock, Docker, CI facts](uv-lock-ci-docker-facts.md) — `test` extra needs `ui` too, opencv collision, action pins, no-daemon build simulation.
- [Sandbox command guards](sandbox-command-guards.md) — .env is untouchable; compound shell/git-adjacent commands get refused; keep calls plain.
- [AppTest + httpx token traps](streamlit-apptest-and-httpx-token-traps.md) — charts are `vega_lite_chart`; h11 error quotes a bad token header; page-test monkeypatch recipe.
- [History routes + persistence wiring](history-routes-and-persistence-wiring.md) — dep-vs-422 ordering, shutdown order, app_factory persistence default, mutation-check recipe.
- [Deployment edge-auth facts](deployment-edge-auth-facts.md) — UI runs the prod gate too, per-router Traefik middlewares, nginx can't inject bearer, compose/helm render quirks.
- [Review-fix traps](review-fixes-traps.md) — limiter double-compares tokens, schema-aware ping via sqlite, wait_for hides cancel-swallowing streams, mutation-script hygiene.
- [Counting switch + test recipes](counting-switch-and-test-recipes.md) — counting_enabled wiring (fail-closed), duplicate test basename trap, folium/AppTest recipes.
