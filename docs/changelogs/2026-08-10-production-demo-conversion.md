# 2026-08-10 — Convert the simulated demo into a real, deployable system

## What

Replaced the simulation with an actual inference pipeline and made the stack deployable
behind Traefik. The repository went from three near-identical Streamlit scripts to a
package with three services.

**New — shared contract** (`src/traffic_ai/`)
`domain.py` (the worker↔UI wire contract), `config.py` (env-driven `Settings`),
`cameras.py` (camera + road-network registry), `store.py` (TTL'd Redis handoff),
`logging.py` (structlog, JSON by default).

**New — inference** (`src/traffic_ai/worker/`)
`detection.py` (`Detector` protocol, `UltralyticsDetector`, `StubDetector`),
`tracking.py` (ByteTrack behind `VehicleTracker`), `counting.py` (`LineCounter`,
`ThroughputWindow`, `derive_congestion`), `annotate.py`, `anpr.py` (seam only),
`pipeline.py` (`CameraPipeline`, decode → detect → track → count → annotate → publish).

**New — service** (`src/traffic_ai/api/`)
FastAPI app with health/readiness, camera state, single-frame JPEG, MJPEG stream, and
crossing events. Owns pipeline startup/shutdown in its lifespan.

**Rewritten — UI** (`src/traffic_ai/ui/`, `Toll_Booth.py`, `pages/Traffic_Analysis.py`)
Thin viewer over the API. `Toll_Booth.py` went from 362 lines to 26.

**New — deployment**
`compose.yaml` (traefik/redis/worker/ui), `docker/Dockerfile.{ui,worker}`, `.env.example`,
`Makefile`, `scripts/fetch_demo_videos.py`, `docs/DEPLOYMENT.md`,
`.github/workflows/ci.yml`.

**Deleted** — `Starter.py` (orphaned pre-multipage version, used the private
`st._config.set_option()` API).

**Rewritten** — `README.md`, `CLAUDE.md`. **Updated** — `.claude/continuity.json` now
carries real `verifyCommands`; `pyproject.toml` restructured with `ui`/`worker` extras.

## Why

The maintainer asked to convert the demo into a production-grade system runnable on a Linux
server via Docker, Compose, and Traefik, with real footage rather than YouTube embeds.

The starting point was a simulation: every number plate, vehicle count, and congestion flag
came from `random.choice()`, the camera feeds were autoplaying YouTube iframes, and the
README described a YOLOv8/DeepSORT/OCR system that had no corresponding code or dependency.

## How

Approach chosen: a separate inference worker with Streamlit as a thin viewer, rather than
inference inside the Streamlit process. Streamlit reruns its whole script on every
interaction, so a UI-owned pipeline restarts on every click, cannot serve a second viewer,
and blocks its own thread — which is exactly what the old `time.sleep()` generators did
(500 iterations at 3s, roughly 25 minutes of frozen UI).

Work was split across four parallel workers over disjoint file sets — pipeline, API, UI,
deployment — with the shared contract written and verified first so every slice compiled
against the same types. Full reasoning for each significant choice is in
`docs/decisions/DECISIONS.md` (eleven entries added).

Decisions worth surfacing here:

- **Detection is behind a `Detector` protocol** because `ultralytics` is AGPL-3.0 while
  this repo is MIT and the artifact is served over a network. `ultralytics` is imported
  inside `UltralyticsDetector.__init__`, never at module scope, so a swap to a
  permissively-licensed model is one file.
- **`supervision` pinned `>=0.30,<0.31`.** Its `ByteTrack` export is deprecated and
  scheduled for removal in 0.31; tracking imports the canonical module path.
- **Redis is a TTL'd cache, not a database.** Persistence is off. A dead worker's state
  expires so the dashboard can say *stale* instead of showing frozen numbers as live.
- **One domain, path-prefix routing.** `${DOMAIN}/api` → worker at router priority 100,
  `${DOMAIN}/` → UI at priority 1. One DNS record, one certificate, no CORS.
- **No plate is ever fabricated.** `plate_text` is `None` unless a real model read it, and
  enabling ANPR without a model raises at startup rather than falling back.

Tradeoff taken deliberately: the Dockerfiles use a stub-package trick to separate the
dependency-install layer from the source layer, rather than a `poetry export` flow. It
gives real layer caching without shipping Poetry in the runtime image, at the cost of being
a less obvious idiom. Flagged rather than hidden.

## Affected modules and behaviors

Everything user-facing changed. The dashboard now shows measured counts instead of
generated ones; the video is a server-annotated MJPEG stream instead of a YouTube iframe;
map corridor colours come from live congestion instead of hardcoded strings; and the UI
degrades visibly — worker unreachable / no data / stale / running — instead of rendering an
empty container.

No authentication was added; there was none before and this remains an unauthenticated
demo. No personal data is stored: no frame, plate, or image is persisted anywhere. Redis
holds only counts, JPEG frames, and crossing events, all TTL'd.

## Intended outcome

`docker compose up` on a Linux server with a DNS record should produce a TLS-served
dashboard showing real vehicle counts over demo footage. The signal that it worked is the
counters advancing as vehicles cross the line in the video, and `/api/readyz` returning 200.
The signal that it did not is a stale banner in the UI or a 503 from readiness — both of
which are now visible rather than silent.

## Verification

Run locally in this worktree:

- `pytest` → **116 passed**, 1 warning (the expected `ByteTrack` `FutureWarning` from the
  pinned `supervision` version).
- `ruff check .` → All checks passed.
- `ruff format --check .` → 64 files already formatted.
- `docker compose config -q` → exit 0, against a temporary `.env`.
- Router priority confirmed in rendered compose output: worker `priority: "100"`,
  ui `priority: "1"`.

**Both images built and run (arm64 host):**

- `docker build -f docker/Dockerfile.ui` → exit 0, 604MB. Verified it contains no torch —
  the extras split does what it is for.
- UI container started; `GET /_stcore/health` → **200 after 2s**.
- `docker build -f docker/Dockerfile.worker` → exit 0, 2.01GB.
- Worker container started against a real Redis. `GET /api/healthz` → **200 after ~25s**
  (first start downloads `yolov8n.pt`). `GET /api/cameras` returned the real registry.
- **Real ultralytics loaded and fetched YOLOv8 weights inside the container** — the
  inference stack is not merely declared, it initialises.
- **`GET /api/readyz` → 503** with `cameras_running: 0, cameras_total: 2` while no video
  files were present. Correct, and it exercises the readiness fix made during integration.
- **Failure path confirmed:** both pipelines logged
  `pipeline_error ... 'could not open video: /data/videos/toll-plaza-a.mp4'` and **the API
  stayed up and kept serving**. This is the "`run()` never raises out" requirement holding
  under a real fault, not just in a test.

**Real inference over real footage — observed:**

Demo videos downloaded (MD5-verified), then redis + worker + ui run together on a Docker
network. Over 45 seconds on `toll-plaza-a`:

- Counts advanced `incoming car 1 → 3`, `outgoing car 1 → 2`; `frames_processed` 32 → 346.
- Crossing events carried real track ids and detection confidences (0.49–0.86).
- `congestion` derived as `free_flow` from measured throughput (`throughput_per_min` 2.0 →
  5.0), not hardcoded.
- `GET /api/cameras/toll-plaza-a/frame.jpg` → 200, `image/jpeg`, 75,593 bytes.
- `GET /api/readyz` → 200, `cameras_running: 2, cameras_total: 2`.
- UI container healthy alongside it (`/_stcore/health` → 200).
- **`plate_text` was `null` on every event** — the only value that appeared. The
  no-fabrication rule holds at runtime, not just in tests.

**This run found a bug that nothing else did.** On the first attempt `toll-plaza-b` failed
continuously with `operator torchvision::nms does not exist` and readiness reported
`cameras_running: 1` of 2. Cause: `docker/Dockerfile.worker` installed `torch` from the
PyTorch CPU wheel index but let `torchvision` resolve from the default index, producing a
mismatched pair. The failure mode is quiet — torchvision imports fine, the container
starts, the healthcheck passes, and it only breaks when a detection needs non-maximum
suppression. Two clean image builds, a green healthcheck, and 116 passing tests all
coexisted with a half-dead detector. Fixed by installing both from the same index in one
command; re-running the identical test gave `cameras_running: 2` with zero `pipeline_error`
and zero NMS errors.

**Still not verified — stated plainly:**

- **`docker compose up` was never run.** Services were exercised individually on a Docker
  network. **Traefik and TLS issuance have not been observed at all** — the routing rules
  and ACME config are validated only by `docker compose config`.
- **The dashboard was never opened in a browser.** The UI container is healthy and the API
  serves it, but no human or automated client has rendered the page, so the map, fragment
  refresh, and MJPEG `<img>` are unobserved in a real browser.
- **Images were built on arm64.** A typical Linux server is amd64, where Compose builds
  fresh. The Dockerfile logic is verified; that exact artifact is not, and the torchvision
  pairing above is precisely the class of thing that can differ per platform.
- Only `toll-plaza-a` was inspected in detail; `toll-plaza-b` was confirmed running via the
  readiness count, not by reading its counters.

## Critical notes

- **`ultralytics` is AGPL-3.0.** This is the single most important follow-up. The interface
  makes the swap cheap, but shipping the default detector in a commercial client demo is a
  licensing decision for the maintainer, not a technical one. Flagged in `README.md`,
  `CLAUDE.md`, and the decision log.
- **First worker start needs outbound network** to download YOLO weights into the
  `model-weights` volume. An air-gapped server needs the weights pre-seeded.
- **Traefik's Docker provider mounts the socket.** It is mounted read-only, but socket
  access is effectively root on the host. Noted in `docs/DEPLOYMENT.md`.
- **Counters reset on worker restart** by design (TTL'd, non-persistent state). If a demo
  needs continuity across a restart, that is a new storage decision.
- **`build_pipelines` constructs one detector per camera.** With `StubDetector` and two
  cameras this is free; with `UltralyticsDetector` it loads the model twice. Sharing one
  detector across pipelines was considered and *not* done, because the pipelines run as
  concurrent asyncio tasks and concurrent inference on a shared model is not guaranteed
  safe. Revisit if camera count grows.
- **Rollback** is `git revert` of this branch plus `docker compose down`; nothing persists,
  so there is no data migration to undo.
- No secret is committed. `.env` and `.streamlit/secrets.toml` are gitignored; only
  `.env.example` is tracked and it carries no values for `DOMAIN` or `ACME_EMAIL`.
