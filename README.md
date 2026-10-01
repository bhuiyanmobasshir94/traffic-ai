# Traffic-AI

Toll-booth and traffic-congestion monitoring for Dhaka. Vehicles are detected and tracked
in video, counted as they cross a virtual line, and the resulting flow is rendered on a
live dashboard with the annotated feed beside it. Crossings are also written to Postgres,
so there is a history view as well as the live one.

Built by [Graaho Technologies](https://graaho.com) as a client-facing demo. It runs on a
single Linux server behind Docker Compose and Traefik, and a Helm chart is provided for
Kubernetes. It processes demo footage, not live camera feeds.

---

## What it actually does

| Capability | Status |
| --- | --- |
| Vehicle detection (car, motorcycle, bus, truck, bicycle) | Working: torchvision by default, YOLO via `ultralytics` opt-in |
| Multi-object tracking across frames | Working: ByteTrack via `supervision` |
| Directional counting (incoming / outgoing, by class) | Working: line crossing, edge-triggered |
| Congestion level from measured flow and density | Working: derived, not hardcoded |
| Annotated live video in the browser | Working: MJPEG, server-side annotation |
| Map with per-corridor congestion colouring | Working: Folium, colour from live state |
| Crossing history in Postgres, history API, Analytics page | Implemented and unit-tested; not yet exercised end to end in a deployed stack |
| Prometheus metrics at `/api/metrics` | Implemented and unit-tested |
| API bearer-token auth, rate limiting, security headers | Implemented and unit-tested |
| Edge login (BasicAuth) in front of the dashboard | Configured in `compose.yaml` and the Helm chart; not exercised against a live proxy |
| **Number-plate recognition (ANPR)** | **Not implemented: a disabled seam only, see below** |
| Speed estimation | Not implemented |
| Toll collection or charging | Not implemented |
| Fine or violation enforcement | Not implemented |

"Working" in this table means the code path exists and ran against the demo footage;
"Implemented" means it is covered by tests but has not been run against a live
deployment. The deployment files themselves have been checked structurally and parsed
(`docker compose config`, `helm lint`, `helm template`), but not run: see the "What is
and is not verified" sections of `docs/DEPLOYMENT.md` and `docs/KUBERNETES.md`.

### On number plates

The pipeline has a `PlateReader` seam (`src/traffic_ai/worker/anpr.py`) and every crossing
event carries a `plate_text` field, but **no plate model ships with this project and no
plate is ever read.** `plate_text` is always `None`, which means *not read*: never
"unreadable", and never a placeholder. The dashboard shows `—` and says the stage is off.

Enabling ANPR without configuring a model is a startup error rather than a silent
fallback, so the system cannot be made to look like it is reading plates when it is not.
Bengali-script plates in particular are not something a general-purpose OCR handles; that
is a modelling project, not a configuration flag.

### What is stored

Postgres holds one row per counted crossing: camera, track id, vehicle class, direction,
timestamp, and detector confidence, with the two plate columns left empty. **No video
frames and no plate images are stored.** Redis holds live state and the latest frame with a
TTL and no persistence. If this ever starts storing imagery, the risk profile in
`CLAUDE.md` has to be rewritten first.

> **Note on history.** Before 2026-08-10 this README described YOLOv8, DeepSORT, OCR, and
> "models fine-tuned on Bangladeshi vehicle datasets" while the code contained none of it:
> every plate and count came from `random.choice()` and the camera feeds were YouTube
> embeds. The detection, tracking, and counting described above are now real. The claims
> that were not backed by code have been removed rather than restated.

---

## Architecture

```
                        :80 / :443
                            |
                     +------v------+
                     |   traefik   |  TLS via Let's Encrypt; BasicAuth login;
                     +------+------+  injects the API bearer token on /api
        Host(${DOMAIN}) && PathPrefix(/api) -> worker   (priority 100)
        Host(${DOMAIN})                     -> ui       (priority 1)
              +-------------+-------------+
              v                           v
      +---------------+          +-----------------+
      | ui (Streamlit)|--------->| worker (FastAPI)|
      |     :8501     |   HTTP   |      :8000      |
      +---------------+  + token +--------+--------+
                                          |  detect -> track -> count -> annotate
                          +---------------+---------------+
                          v                               v
                   +-------------+                +---------------+
                   |    redis    |                |   postgres    |
                   | live state, |                | crossing      |
                   | TTL'd       |                | history       |
                   +-------------+                +---------------+
                                                  schema: migrate (one-shot
                                                  `alembic upgrade head`)
```

Inference runs in the worker, never in the UI. Streamlit reruns its entire script on every
interaction, so a pipeline owned by the UI would restart on every click, lose its counters,
and be unable to serve a second viewer. The worker owns the loop; the UI reads the latest
result and the browser streams the annotated video directly.

Redis holds live state with a TTL and no persistence. If the worker dies its state expires, so
the dashboard reports *stale* rather than showing frozen numbers as though they were live.
Postgres is the durable record of crossings. The live path does not depend on it: a database
that is down degrades history, never the dashboard.

Authentication has two layers. People log in once at Traefik (BasicAuth); the worker
separately requires a bearer token on every `/api` route except the two health probes. After
the login, Traefik supplies the token toward the worker, which is what lets a browser `<img>`
(unable to send headers) play the MJPEG stream. The UI sends the token itself on its own
calls. Details and the reasoning are in `docs/DEPLOYMENT.md`.

| Module | Responsibility |
| --- | --- |
| `src/traffic_ai/domain.py` | The contract between worker and UI. Changing it is an API change. |
| `src/traffic_ai/config.py` | Environment-driven settings; refuses an unsafe production configuration at startup. |
| `src/traffic_ai/cameras.py` | Camera registry and map geometry: coordinates live there once. |
| `src/traffic_ai/worker/` | Detection, tracking, counting, annotation, pipeline loop. |
| `src/traffic_ai/api/` | FastAPI service; owns pipeline startup and shutdown, auth, rate limiting, headers. |
| `src/traffic_ai/db/` | Postgres models, repository, and the batching writer. |
| `src/traffic_ai/metrics.py` | Prometheus metrics. |
| `src/traffic_ai/ui/` | Streamlit viewer: API client, components, shared dashboard, analytics. |
| `migrations/` | Alembic revisions for the Postgres schema. |
| `deploy/helm/traffic-ai/` | Helm chart (worker and UI; Redis and Postgres are external). |

---

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/), or just Docker.

```bash
# 1. Fetch the demo footage (~65 MB, MD5-verified)
make videos

# 2a. Whole stack in Docker: needs a real domain, a .env, and a users file.
#     Follow docs/DEPLOYMENT.md; do not skip the secrets steps.

# 2b. Or locally, in two shells (needs a Redis on :6379)
make sync                                   # uv sync --locked, all extras
python -m traffic_ai.api                    # worker -> http://localhost:8000
streamlit run Toll_Booth.py                 # UI     -> http://localhost:8501
```

Locally the environment is `development`: no token is required and Postgres is optional. A
database that is missing, misconfigured, or unreachable costs history, never the live
dashboard (the pipeline's database writes are fail-open), so without one the Analytics page
reports history as unavailable. Point `TRAFFIC_AI_DATABASE_URL` at a
Postgres and run `alembic upgrade head` to get history locally. Without uv, `make
install-pip` installs the same extras (it resolves fresh and ignores `uv.lock`).

The Docker path is not a one-liner any more, by design. `docker compose up` needs
`TRAFFIC_AI_API_TOKEN` and `POSTGRES_PASSWORD` set, and `config/traefik/users.htpasswd`
created. The production configuration refuses to start without them rather than running
open. **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** is the complete runbook: DNS, secrets,
first boot, migrations, verification, backups, upgrades, and troubleshooting.

### Deploying

- **Single server (Docker Compose):** [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- **Kubernetes (Helm):** [docs/KUBERNETES.md](docs/KUBERNETES.md). Read its "Edge
  authentication" section first: how much of the login can work depends on the ingress
  controller.

### Demo footage

The two demo clips come from the Roboflow `supervision` example assets and are downloaded
at setup time, not committed. Provenance is recorded in `data/videos/ATTRIBUTION.md`.
Replace them with your own footage by dropping files with the same names into
`data/videos/`: the filenames are declared in `src/traffic_ai/cameras.py`.

---

## Development

```bash
make test                           # pytest; no torch or GPU required
make lint                           # ruff check + ruff format --check
```

The test suite deliberately does **not** depend on `torch` or `ultralytics`. Detection runs
behind a `Detector` protocol with a deterministic `StubDetector` for tests, so the suite is
fast and runs anywhere. Tests marked `requires_inference` skip unless the real stack is
installed, and the database tests skip unless a Postgres is supplied. The Helm tests skip
where `helm` is not installed.

### Configuration

Every setting is environment-driven with a working default, prefixed `TRAFFIC_AI_`: see
`src/traffic_ai/config.py` for the full list. The ones that matter for performance on a
small server are `TRAFFIC_AI_TARGET_FPS`, `TRAFFIC_AI_DETECT_EVERY_N_FRAMES`, and
`TRAFFIC_AI_FRAME_WIDTH`. `TRAFFIC_AI_DETECTOR` picks the detection backend (see the
Licensing note below). In production (`TRAFFIC_AI_ENVIRONMENT=production`, which both
deployment paths set), `TRAFFIC_AI_API_TOKEN` is required, at least 32 characters, and the
database URL may not carry the development password.

---

## Licensing note

This repository is MIT (see `LICENSE`). The default detector is `torchvision`
(BSD-3-Clause), so the default deployment path carries no AGPL obligation.
`ultralytics` (YOLOv8) is AGPL-3.0, and the AGPL's network-use clause reaches software
served over a network: it stays fully supported as an explicit opt-in
(`TRAFFIC_AI_DETECTOR=ultralytics`), but choosing it takes on that obligation. Detection
sits behind the `Detector` protocol in `src/traffic_ai/worker/detection.py`, so both
backends, and any future one, are a `TRAFFIC_AI_DETECTOR` setting away, not a rewrite.
Take your own legal advice before deploying this commercially with either detector.

---

*Capabilities table and module map checked against the code on branch `production-hardening`,
base commit `5c67d11` plus uncommitted working-tree changes, 2026-10-01. If you are reading
this much later, check `git log` before trusting it.*
