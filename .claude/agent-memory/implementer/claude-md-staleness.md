---
name: claude-md-staleness
description: traffic-ai's root CLAUDE.md and docs/decisions/DECISIONS.md describe the simulation-only demo era and lag behind an in-flight rewrite; cross-check against pyproject.toml / directory contents before trusting a claim like "no tests exist here".
metadata:
  type: project
---

As of the UI-rewrite task (worker split into `ui`/`api`/`worker` behind an HTTP contract in
`src/traffic_ai/domain.py`), the committed `CLAUDE.md` still says "no test, lint, format ...
command exists in this project" and DECISIONS.md still frames the dashboard as pure
simulation. Neither is true anymore: `pyproject.toml` already has `pytest`, `ruff`,
`pytest-asyncio`, `fakeredis` as dev dependencies and a `[tool.ruff]` / `[tool.pytest.ini_options]`
config, and `tests/conftest.py` exists with real fixtures (`fakeredis`-backed `StateStore`,
`Settings` with `_env_file=None`).

**Why:** the repo is mid-migration; orchestrator task packets for this phase are the current
source of truth for verification commands, not the committed docs, which get updated at the
end of the phase, not the start.

**How to apply:** when a root `CLAUDE.md` claim conflicts with what a task packet asks you to
run, check the actual repo state (`pyproject.toml`, directory listing) before treating the
doc as binding — but still flag the discrepancy rather than silently overriding it, since a
future session reading only `CLAUDE.md` will hit the same stale claim.
