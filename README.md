# Traffic-AI

Toll-booth and traffic-congestion monitoring for Dhaka. Vehicles are detected and tracked
in video, counted as they cross a virtual line, and the resulting flow is rendered on a
live dashboard with the annotated feed beside it.

Built by [Graaho Technologies](https://graaho.com) as a client-facing demo. It runs on a
single Linux server behind Docker Compose and Traefik.

---

## What it actually does

| Capability | Status |
| --- | --- |
| Vehicle detection (car, motorcycle, bus, truck, bicycle) | Working — YOLO via `ultralytics` |
| Multi-object tracking across frames | Working — ByteTrack via `supervision` |
| Directional counting (incoming / outgoing, by class) | Working — line crossing, edge-triggered |
| Congestion level from measured flow and density | Working — derived, not hardcoded |
| Annotated live video in the browser | Working — MJPEG, server-side annotation |
| Map with per-corridor congestion colouring | Working — Folium, colour from live state |
| **Number-plate recognition (ANPR)** | **Not implemented — see below** |
| Speed estimation | Not implemented |
| Automated toll charging / fine enforcement | Not implemented |

### On number plates

The pipeline has a `PlateReader` seam (`src/traffic_ai/worker/anpr.py`) and every crossing
event carries a `plate_text` field, but **no plate model ships with this project and no
plate is ever read.** `plate_text` is always `None`, which means *not read* — never
"unreadable", and never a placeholder. The dashboard shows `—` and says the stage is off.

Enabling ANPR without configuring a model is a startup error rather than a silent
fallback, so the system cannot be made to look like it is reading plates when it is not.
Bengali-script plates in particular are not something a general-purpose OCR handles; that
is a modelling project, not a configuration flag.

> **Note on history.** Before 2026-08-10 this README described YOLOv8, DeepSORT, OCR, and
> "models fine-tuned on Bangladeshi vehicle datasets" while the code contained none of it —
> every plate and count came from `random.choice()` and the camera feeds were YouTube
> embeds. The detection, tracking, and counting described above are now real. The claims
> that were not backed by code have been removed rather than restated.

---

## Architecture

```
                        :80 / :443
                            │
                     ┌──────▼──────┐
                     │   traefik   │  TLS via Let's Encrypt
                     └──────┬──────┘
        Host(${DOMAIN}) && PathPrefix(/api) → worker   (priority 100)
        Host(${DOMAIN})                     → ui       (priority 1)
              ┌─────────────┴─────────────┐
              ▼                           ▼
      ┌───────────────┐          ┌─────────────────┐
      │ ui (Streamlit)│─────────▶│ worker (FastAPI)│
      │     :8501     │   HTTP   │      :8000      │
      └───────────────┘          └────────┬────────┘
                                          │  detect → track → count → annotate
                                          ▼
                                   ┌─────────────┐
                                   │    redis    │  TTL'd state + frames
                                   └─────────────┘
```

Inference runs in the worker, never in the UI. Streamlit reruns its entire script on every
interaction, so a pipeline owned by the UI would restart on every click, lose its counters,
and be unable to serve a second viewer. The worker owns the loop; the UI reads the latest
result and the browser streams the annotated video directly.

Redis holds state with a TTL and no persistence. If the worker dies its state expires, so
the dashboard reports *stale* rather than showing frozen numbers as though they were live.

| Module | Responsibility |
| --- | --- |
| `src/traffic_ai/domain.py` | The contract between worker and UI. Changing it is an API change. |
| `src/traffic_ai/cameras.py` | Camera registry and map geometry — coordinates live here once. |
| `src/traffic_ai/worker/` | Detection, tracking, counting, annotation, pipeline loop. |
| `src/traffic_ai/api/` | FastAPI service; owns pipeline startup and shutdown. |
| `src/traffic_ai/ui/` | Streamlit viewer: API client, components, one shared dashboard. |

---

## Quick start

Requires Python 3.12+ and Poetry, or just Docker.

```bash
# 1. Fetch the demo footage (~65 MB, MD5-verified)
make videos

# 2a. Whole stack in Docker
cp .env.example .env        # set DOMAIN and ACME_EMAIL
docker compose up --build

# 2b. Or locally, in two shells (needs a Redis on :6379)
poetry install --extras "ui worker"
python -m traffic_ai.api                    # worker → http://localhost:8000
streamlit run Toll_Booth.py                 # UI     → http://localhost:8501
```

For deploying to a real server with TLS, see **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

### Demo footage

The two demo clips come from the Roboflow `supervision` example assets and are downloaded
at setup time, not committed. Provenance is recorded in `data/videos/ATTRIBUTION.md`.
Replace them with your own footage by dropping files with the same names into
`data/videos/` — the filenames are declared in `src/traffic_ai/cameras.py`.

---

## Development

```bash
pytest                              # 116 tests; no torch or GPU required
ruff check . && ruff format --check .
```

The test suite deliberately does **not** depend on `torch` or `ultralytics`. Detection runs
behind a `Detector` protocol with a deterministic `StubDetector` for tests, so the suite is
fast and runs anywhere. Tests marked `requires_inference` skip unless the real stack is
installed.

### Configuration

Every setting is environment-driven with a working default, prefixed `TRAFFIC_AI_` — see
`src/traffic_ai/config.py` for the full list. The ones that matter for performance on a
small server are `TRAFFIC_AI_TARGET_FPS`, `TRAFFIC_AI_DETECT_EVERY_N_FRAMES`, and
`TRAFFIC_AI_FRAME_WIDTH`.

---

## Licensing note

This repository is MIT (see `LICENSE`). **`ultralytics`, the default detector, is
AGPL-3.0**, and the AGPL's network-use clause reaches software served over a network.
Detection therefore sits behind the `Detector` protocol in `src/traffic_ai/worker/detection.py`,
so swapping to a permissively-licensed model (YOLOX, RT-DETR — both Apache-2.0) is a
single-file change. Take your own legal advice before deploying this commercially with the
default detector.

---

*Capabilities table and module map verified against the code on branch
`worktree-production-demo`, 2026-08-10. If you are reading this much later, check `git log`
before trusting it.*
