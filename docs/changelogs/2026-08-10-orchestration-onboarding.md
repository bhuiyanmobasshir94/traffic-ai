# 2026-08-10 — Orchestrator–worker onboarding

## What

Added the orchestration and continuity scaffolding this repository had none of:

- `CLAUDE.md` — project context, operating constraints, non-negotiables, orchestration routing.
- `.claude/rules/streamlit-app.md` — path-scoped standards for `*.py` and `pages/*.py`.
- `.claude/settings.json` — permission deny/ask list.
- `.claude/continuity.json` — changelog and decision-log paths.
- `docs/decisions/DECISIONS.md` — seeded with five standing decisions.
- `docs/changelogs/README.md` — entry format.
- `.gitignore` — added `.claude/settings.local.json`, `.claude/worker-ledger.jsonl`,
  `.claude/worktrees/`, and `.streamlit/secrets.toml`.

## Why

Ran `/orchestration-onboard`. The repository had no `CLAUDE.md`, `AGENTS.md`, `.cursorrules`,
or `.github/copilot-instructions.md`, and no changelog or decision log — so every session
started by re-deriving what the project is, and repeatedly hit the same trap: the README
describes a YOLOv8/DeepSORT/OCR system that does not exist in the code.

## How

Explored the repository, then interviewed the maintainer for what the code cannot show.
Findings that shaped the file:

- All telemetry is `random.choice()`. There is no computer vision anywhere in the repo, and no
  `ultralytics`, `opencv`, `torch`, or OCR dependency. The README is a product pitch.
- `feature-realtime` is zero commits ahead of `main` — identical trees. Last code commit was
  2024-02-23.
- Both live page files define *both* page functions but call only one, so two of four
  implementations are dead code and have already diverged.
- The first 155 lines are byte-identical across all three Python files.
- Zero `except` clauses in the entire repository.

Two of the three template rule files were dropped rather than adapted: their path globs
(`**/migrations/**`, `**/models/**`, `**/*.sql`, `**/jobs/**`, `**/queues/**`, `**/views.py`,
`src/**`, `app/**`, `api/**`) matched **zero files**. The genuinely applicable content —
secrets, untrusted input, blocking work, observability — was folded into the single
Streamlit-scoped rule instead. A rule that is present but untrue teaches the reader to ignore
the file.

`verifyCommands` was deliberately omitted from `.claude/continuity.json`: there are no tests,
no linter, and no CI, so there is no command to name.

## Affected modules and behaviors

Documentation and agent configuration only. **No application code was modified**; runtime
behavior is unchanged.

## Intended outcome

A session opening this repository should learn within one file that the committed code is a
simulation, that the README is aspirational, and which of the four page functions actually
execute — without rediscovering any of it. The signal that this worked is a session that does
not go looking for the detector.

## Verification

- `diff <(head -160 Toll_Booth.py) <(head -160 pages/Traffic_Analysis.py)` → identical.
- `diff <(head -155 Toll_Booth.py) <(head -155 Starter.py)` → identical.
- `grep -n` confirmed `Toll_Booth.py:191` calls `show_toll_booth_page()` and
  `pages/Traffic_Analysis.py:191` calls `show_congestion_page()`, leaving the other definition
  in each file unreachable.
- `grep -c except` → `0` in all three Python files.
- `git log --oneline main..feature-realtime` → empty.

No test or lint command was run, because none exists in this project.

## Critical notes

- **`.streamlit/secrets.toml` was previously not gitignored.** It is now. No secret was ever
  committed — the repository contains no credentials — but the gap would have caught the first
  camera credential added.
- The `CLAUDE.md` written here describes the repository as it stands at this commit: a
  simulation. Conversion work to a real inference pipeline follows in this same branch and
  will supersede several sections of it.
- Nothing here changes application behavior, so there is nothing to roll back operationally.
  Reverting the commit simply removes the documentation.
