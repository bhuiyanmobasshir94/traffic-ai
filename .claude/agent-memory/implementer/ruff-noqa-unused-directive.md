---
name: ruff-noqa-unused-directive
description: In traffic-ai, noqa comments for rule codes not in pyproject.toml's select list (e.g. BLE001, PLC0415) trigger RUF100 "unused noqa" — use plain comments instead.
metadata:
  type: project
---

`pyproject.toml`'s `[tool.ruff.lint] select` is `E, F, I, B, UP, S, C4, SIM, RUF` (S104
ignored). Codes from unselected plugins — `BLE001` (flake8-blind-except), `PLC0415`
(pylint import-outside-toplevel) — are not checked, so `# noqa: BLE001` on a broad
`except Exception:` or `# noqa: PLC0415` on a lazy import is flagged by `RUF100` as an
unused directive and fails `ruff check`.

**Why:** verified directly — `ruff check --isolated --select E,F,I,B,UP,S,C4,SIM,RUF` on a
minimal broad-except reproduction flags `RUF100 Unused noqa directive (non-enabled:
BLE001)`. An earlier read of `store.py` in the same session showed a `# noqa: BLE001`
comment, but that turned out to be stale context from before the file was rewritten by
another worker — the current `store.py` uses a plain comment (`# A probe reports False; it
does not crash the caller...`) with no noqa tag at all. See
[[anchor-files-can-change-between-resumed-sessions]].

**How to apply:** when adding a broad `except Exception:` (readiness probes, retry loops,
"must never crash the caller" spots) or a lazy `from ultralytics import X` inside
`__init__`, explain the rationale in a plain comment, not a `noqa` tag. Only add `noqa` for
codes actually in the `select` list — check `pyproject.toml` first if unsure.
