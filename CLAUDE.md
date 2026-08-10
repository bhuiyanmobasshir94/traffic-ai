# CLAUDE.md — traffic-ai

Toll-booth and traffic-congestion monitoring for Dhaka, built as a Graaho client-facing
demo. Vehicles are detected, tracked, and counted for real: a worker decodes demo footage,
runs a YOLO detector and a ByteTrack tracker over it, counts line crossings by direction,
and publishes state and annotated frames. Streamlit is a thin viewer over that state.

**The numbers on the dashboard are measured, not generated.** Until 2026-08-10 they were
`random.choice()` and the README described a system that did not exist. That is no longer
true, and it must not become true again: nothing in this repository may present a
fabricated value as a measurement. The one deliberate gap is number-plate reading — see
the ANPR note under Non-negotiables.

It is still a demo. No user depends on it, no money moves, and it stores nothing. But it
now processes video and runs a real pipeline, so a bug can burn CPU, wedge a container, or
put a wrong number in front of a client.

Detailed standards live in `.claude/rules/` and load when you touch the paths they cover:

| Rule | Loads when working on |
| --- | --- |
| `rules/streamlit-app.md` | the dashboard source (`*.py`, `pages/*.py`) |

## Operating constraints

- **Three services, one contract.** `worker` (inference + FastAPI) and `ui` (Streamlit)
  share only `src/traffic_ai/domain.py`. Changing a field there is an API change and both
  sides move together. The UI never imports the worker; it talks HTTP.
- **Video decode is blocking and must stay off the event loop.** `cv2.VideoCapture.read()`
  runs via `asyncio.to_thread` in `worker/pipeline.py`. The whole rewrite exists because the
  old code slept on the script thread; never reintroduce `time.sleep` in a serving path.
- **Redis is a cache with a TTL, not a database.** Every key expires
  (`state_ttl_seconds`, default 30). Persistence is off. Counters reset when the worker
  restarts — by design, so a dead worker cannot leave a dashboard that looks live. The UI
  distinguishes live / stale / no-data and must keep doing so.
- **`ultralytics` is AGPL-3.0 and this repo is MIT.** Detection sits behind the `Detector`
  Protocol in `worker/detection.py`. `Settings.detector` (`TRAFFIC_AI_DETECTOR`) defaults
  to `"torchvision"` (BSD-3-Clause, `TorchvisionDetector`), so the default deployment path
  carries no AGPL obligation; `"ultralytics"` (`UltralyticsDetector`) is a fully supported
  explicit opt-in. Both import their library inside `__init__`, never at module scope. Do
  not call `torch`, `torchvision`, or `ultralytics` from pipeline, API, or UI code.
- **`supervision` is pinned `>=0.30,<0.31`.** Its `ByteTrack` export is deprecated and
  scheduled for removal in 0.31. `worker/tracking.py` imports the canonical module path and
  wraps it in `VehicleTracker`, so the eventual swap is one file. The `FutureWarning` the
  suite emits comes from the library's own constructor and is expected.
- **Inference is CPU-bound and the server is small.** Two streams at `target_fps=12` with
  `detect_every_n_frames=2`. If the demo is sluggish, tune those and `frame_width` before
  changing anything structural — `docs/DEPLOYMENT.md` says which knob does what.
- **The stack is one domain.** Traefik routes `${DOMAIN}/` to the UI and `${DOMAIN}/api` to
  the worker, with an explicit router priority so `/api` wins. One DNS record, one
  certificate, no CORS. Moving the API to its own hostname means revisiting `api_public_url`
  and the CORS posture.

### What a new engineer gets wrong in the first week

- **Assuming the README is current.** It was aspirational marketing for two years. It has
  been rewritten against the code, but check `git log` before trusting any claim in it.
- **Adding a fabricated value "just for the demo."** A placeholder plate, a sample count, a
  seeded random. This is the specific failure the rewrite exists to fix.
- **Editing `pyproject.toml` extras carelessly.** `ui` and `worker` are separate on purpose:
  the UI image must never pull torch. Adding a shared dependency to the wrong place quietly
  doubles the image.
- **Running the suite expecting inference.** `ultralytics`/`torch` are deliberately not a
  test dependency. Detection is stubbed (`StubDetector`); tests marked
  `requires_inference` skip unless the stack is importable.

## Non-negotiables

- **No fabricated data.** No random, placeholder, or "representative" value may reach the
  UI or the API as though it were measured.
- **No plate is ever invented.** `CrossingEvent.plate_text` is `None` unless a real model
  read it; `None` means not read, never unreadable and never a stand-in.
  `build_plate_reader` raises when ANPR is enabled without a model — it fails closed rather
  than falling back. No plate model ships with this project.
- **No plaintext secrets.** `.env` and `.streamlit/secrets.toml` are gitignored. Only
  `.env.example` is tracked, and it carries no values for `DOMAIN` or `ACME_EMAIL`.
- **`camera_id` is an allowlist boundary.** It arrives from map clicks and URLs. Resolve it
  through `cameras.get_camera()`, which returns `None`; a miss is a 404, never an
  interpolation into a Redis key or a path.
- **Nothing user- or config-derived reaches `unsafe_allow_html` unescaped.** The one
  markup builder is `ui/components.py::_build_stream_markup` — allowlist first, then
  `html.escape`. Keep it the only one.
- **Redis and the worker are never published to the host.** Only Traefik binds ports.
- **Fail visibly.** `CameraPipeline.run()` never raises out: it records the fault on
  `CameraState.error` and keeps publishing so the UI can show it. No bare `except: pass`
  anywhere.

## Orchestration in this repository

Global roles are defined in `~/.claude/CLAUDE.md`. Repo-specific routing:

- **Always dispatch `reviewer` before integrating** changes to `worker/pipeline.py`,
  `worker/anpr.py`, credential handling, or anything that would store a frame or a plate.
  Real vehicle imagery is personal data; this repo does not store any today, and the risk
  profile in this file must be rewritten before it does.
- **Route to `implementer`, not `fast-implementer`,** for `domain.py` — it is the contract
  between two services, so a "mechanical" edit there is a two-sided API change.
- **Do not parallelize across** `domain.py`, `config.py`, and `cameras.py`. Everything
  depends on them; concurrent edits produce a contract that no slice agrees on.

## Session changelog

Every session that changes code, configuration, or documentation writes an entry to
`docs/changelogs/` and commits it with the change it describes.

## Commands

```bash
# Install (core + both extras + dev tooling)
poetry install --extras "ui worker"

# Fetch the demo footage (~65MB, MD5-verified). Required before first run.
make videos

# Verify
pytest                      # full suite; no torch required
ruff check . && ruff format --check .

# Run the whole stack locally (needs .env — copy .env.example)
docker compose up --build

# Run one service against a local Redis
python -m traffic_ai.api                             # worker on :8000
streamlit run Toll_Booth.py                          # UI on :8501
```

Deployment is `docs/DEPLOYMENT.md`. Do not invent commands that are not in these two places.

## Architecture

`worker/pipeline.py` owns one `CameraPipeline` per camera: decode a frame, detect every
Nth frame, track on every frame, count crossings against a normalized line, derive
congestion from measured flow and density, annotate, publish. It writes `CameraState`,
a JPEG, and `CrossingEvent`s to Redis through `store.py`, all TTL'd.

`api/` serves that state — JSON, a single frame, and an MJPEG stream — and owns pipeline
startup and shutdown in its lifespan. It imports the worker lazily so the API module can be
loaded, and tested, without the inference stack.

`ui/` reads the API over HTTP. `dashboard.py` renders one layout used by both pages;
`Toll_Booth.py` and `pages/Traffic_Analysis.py` are ~25-line entrypoints that pick a camera.
Live regions refresh via `@st.fragment(run_every="2s")`; the MJPEG `<img>` sits outside the
fragment so the browser streams it directly and Streamlit never handles a frame.

`cameras.py` is the single registry of cameras and map geometry — coordinates live there
once, as `[lat, lon]`.
