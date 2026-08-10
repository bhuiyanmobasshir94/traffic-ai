# CLAUDE.md — traffic-ai

A Streamlit dashboard for highway toll-booth monitoring and traffic congestion analysis in
Dhaka, built as a Graaho client-facing demo. **What is committed here is a simulation, not a
system.** Every number plate, vehicle count, and congestion flag comes from `random.choice()`;
the "camera feeds" are autoplaying YouTube iframes. There is no computer vision, no inference,
no database, no authentication, and no persistence anywhere in this repository.

The README describes YOLOv8, DeepSORT, OCR, and "models fine-tuned on Bangladeshi vehicle
datasets." **None of that exists in this codebase** — there is no `ultralytics`, `opencv`,
`torch`, or OCR dependency in `pyproject.toml` or `requirements.txt`. Treat the README as a
product pitch, not a description of the code. Do not go looking for a detector.

This repo is the base for the real build: replacing the simulation with actual camera ingest
and inference. That transition is the point of most work here, so weigh changes by whether
they make it easier or harder.

Detailed standards live in `.claude/rules/` and load when you touch the paths they cover:

| Rule | Loads when working on |
| --- | --- |
| `rules/streamlit-app.md` | the dashboard source (`*.py`, `pages/*.py`) |

## Operating constraints

- **Streamlit reruns the whole script on every interaction.** There is no event loop and no
  partial update. Any widget click restarts the page script from line 1 and all counters
  reset. Anything that must survive an interaction lives in `st.session_state`.
- **The current data generators block the script thread.** `yield_data()` and
  `simulate_vehicles()` (`Toll_Booth.py:113`, `:128`) sleep in-loop feeding `st.write_stream`,
  500 iterations at 3s and 2s — roughly 25 and 17 minutes of frozen UI. Real ingest must not
  follow this pattern: it runs off the script thread, and the UI reads the latest result
  rather than driving the loop.
- **The UI is pinned to Streamlit internals.** Layout depends on hardcoded pixel heights
  (`st.container(height=450|270|735)`) and CSS targeting unversioned test IDs —
  `stSidebarNav`, `stAppViewBlockContainer`, `toastContainer` (`Toll_Booth.py:166-186`). Pins
  are frozen at `streamlit==1.31.1` (Feb 2024). A dependency upgrade is a UI rewrite, not a
  version bump; treat it as its own task with a visual check.
- **There is nowhere safe to put a secret yet.** `.gitignore` covers `.env` but not
  `.streamlit/secrets.toml`. Before the first camera credential, RTSP URL, or API key lands,
  that file must be gitignored. Assume anything added carelessly gets committed.
- **Nothing here is load-bearing today.** No user depends on it, no money moves, no personal
  data is stored. A bug is a broken demo, not an incident. That changes the moment real feeds
  arrive — a photographed number plate is personal data, and the risk profile in this file
  must be revised at that point rather than inherited.

### What a new engineer gets wrong in the first week

- **Editing the page function that never runs.** Both live files define *both* page
  functions but call only one: `Toll_Booth.py:191` calls `show_toll_booth_page()`, leaving
  `show_congestion_page()` at `:279` dead; `pages/Traffic_Analysis.py:191` calls
  `show_congestion_page()`, leaving `show_toll_booth_page()` at `:194` dead. The dead copies
  have already diverged. Check what `main()` calls before editing.
- **Touching `Starter.py`.** It is the orphaned pre-multipage version — nothing imports or
  runs it. Its name makes it look like the entrypoint; the entrypoint is `Toll_Booth.py`.
- **Running from the wrong directory.** `logo.png` loads by relative path; the app only
  starts from the repository root.
- **Trying to debug the detector.** There is no detector.

## Non-negotiables

- **No plaintext secrets** in code, configuration, or committed files — and
  `.streamlit/secrets.toml` stays gitignored.
- **Guard lookups on user-driven values.** `MAPPER[st_data["last_object_clicked_popup"]]`
  (`Toll_Booth.py:261`) is truthiness-checked but not membership-checked; a new marker or a
  renamed popup raises `KeyError`. New lookups on click payloads use `.get()` with a fallback.
- **Nothing user-controlled reaches `unsafe_allow_html=True`.** The iframe blocks build HTML
  by string concatenation. The URLs are constants today; the moment one comes from input,
  config, or an API, it is an injection vector.
- **Do not add a sixth copy.** The map-render block already appears five times and the iframe
  HTML six. Extract to a shared module before duplicating again.
- **Verify by running the app.** There are no tests, no linter, and no CI in this repo. A
  claim that something works means it was launched and looked at, and the report says so.
  Never claim a check you did not run — there is no suite to hide behind.

## Orchestration in this repository

Global roles are defined in `~/.claude/CLAUDE.md`. Repo-specific routing:

- **Always dispatch `reviewer` before integrating** any change introducing real camera
  ingest, credential handling, or plate/image storage. The first code that touches real
  vehicle data is the first code here with a genuine blast radius.
- **Route to `implementer`, not `fast-implementer`,** for anything touching the duplicated
  page functions. A "mechanical" edit there lands in the dead copy.
- **Do not parallelize across** `Toll_Booth.py`, `pages/Traffic_Analysis.py`, and
  `Starter.py`. Their first 155 lines are byte-identical; concurrent workers produce
  divergent copies of the same helper.

## Session changelog

Every session that changes code, configuration, or documentation writes an entry to
`docs/changelogs/` and commits it with the change it describes.

## Commands

```bash
# Install
poetry install

# Run — must be from the repository root
streamlit run Toll_Booth.py --server.runOnSave true

# Regenerate requirements.txt after a dependency change
poetry export --without-hashes --format=requirements.txt > requirements.txt
```

No test, lint, format, or migration command exists in this project. Do not invent one, and do
not report one as run.

## Architecture

`Toll_Booth.py` is the entrypoint; Streamlit's `pages/` convention auto-registers
`pages/Traffic_Analysis.py` as the second nav entry. Each page renders the same three regions:
a video container holding a YouTube iframe, a Folium map with two toll-plaza markers and three
congestion polylines, and a right-hand chat column streaming generated telemetry. Clicking a
marker returns its popup text through `st_folium`, which maps to a video URL held in
`st.session_state` — `VIDEO_URL` on the toll page, `T_VIDEO_URL` on the analysis page, so the
two do not clobber each other. There is no shared module: coordinates, helpers, and generators
are copy-pasted identically into all three Python files. Clearing that is the main structural
debt before real ingest lands.
