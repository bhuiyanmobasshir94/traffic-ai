# Decision log

Append-only. Each entry: date, decision, why, and what it rules out. Rejected alternatives
matter as much as the choice — most wasted sessions are a rediscovery of an option that was
already ruled out for a reason nobody wrote down.

---

## 2024-02-23 — Streamlit multipage via the `pages/` directory convention

**Decision.** Navigation is Streamlit's filesystem convention: `Toll_Booth.py` is the
entrypoint and `pages/Traffic_Analysis.py` is auto-registered as the second nav entry.

**Why.** Replaced the earlier single-file design in `Starter.py`, which put both views behind
a `st.sidebar.radio("Services", ...)` selector. The radio approach re-rendered everything on
switch and could not deep-link to a view.

**Rules out.** The `st.navigation` / `st.Page` programmatic API, and any return to a
radio-driven single page. `Starter.py` is retained but dead; it is not a second entrypoint.
Reintroducing programmatic navigation means migrating both pages together, not one.

---

## 2024-02-23 — Camera feeds are YouTube iframes, as placeholders

**Decision.** Each page renders its "camera feed" as an autoplaying, muted, looping YouTube
`<iframe>` injected through `st.write(..., unsafe_allow_html=True)`.

**Why.** The demo needed continuous plausible footage with no infrastructure. YouTube provided
hosting, transcoding, and looping for free.

**Rules out.** Treating the current video path as the ingest path. It carries no frames into
Python — nothing can be run over it. Real ingest is a separate mechanism, not an extension of
this one. It also means the demo silently depends on YouTube availability and on the client's
network not blocking it.

---

## 2024-02-23 — Telemetry is simulated; no persistence layer chosen

**Decision.** Vehicle counts, number plates, and congestion flags are produced by in-process
generators using `random`. Nothing is stored: no database, no cache, no file writes.

**Why.** The dashboard was built to demonstrate the intended UI ahead of any inference
pipeline existing.

**Rules out.** Assuming any storage exists or that a schema has been agreed. There is no
prior art to match when persistence is added — that is an open decision, not a settled one.
It also means no historical query, no replay, and no cross-session continuity is possible
today.

---

## 2024-02-23 — UI layout is pinned to Streamlit internal selectors

**Decision.** Layout relies on hardcoded pixel container heights and CSS targeting Streamlit's
internal test IDs (`stSidebarNav`, `stSidebarNavSeparator`, `stAppViewBlockContainer`,
`toastContainer`). Dependencies are pinned at `streamlit==1.31.1`.

**Why.** Streamlit exposed no supported API for the density and chrome-hiding the demo needed.

**Rules out.** Treating a Streamlit upgrade as routine maintenance. These selectors are
unversioned internals with no compatibility guarantee, so a version bump is a UI change
requiring visual verification, and must be scoped as its own piece of work.

---

## 2026-08-10 — Orchestrator–worker onboarding; no verification commands recorded

**Decision.** Onboarded the repository with `CLAUDE.md`, one path-scoped rule file
(`.claude/rules/streamlit-app.md`), and this decision log. `.claude/continuity.json` records
the changelog and decision paths but deliberately omits `verifyCommands`.

**Why.** The repository has no tests, no linter, no formatter, and no CI. Listing a command
that does not exist would make the verification reminder fire against nothing and teach every
future session to report a check it never ran.

**Rules out.** Claiming any automated verification in this repo until a suite actually exists.
Until then, "verified" means the app was launched and looked at, and the report says so.
Dropped the template's `data-and-migrations` and `reliability-and-performance` rule files
entirely: their path globs matched zero files, as this project has no models, migrations,
SQL, jobs, queues, or request handlers.

---

## 2026-08-10 — Inference runs in a separate worker; Streamlit becomes a thin viewer

**Decision.** Split the single Streamlit process into a FastAPI worker that owns video
decode, detection, tracking, and counting, and a Streamlit UI that only reads results over
HTTP. Redis carries the handoff. Rejected: running inference inside the Streamlit process.

**Why.** Streamlit reruns its whole script on every interaction, so a pipeline owned by the
UI restarts and loses all state on any click, cannot serve a second viewer, and blocks the
thread while it works — which is exactly the failure the previous `time.sleep()` generators
exhibited.

**Rules out.** Any design where the UI drives the capture loop, and any per-session
inference state. It also means the UI must degrade visibly when the worker is down rather
than assuming a result is always available.

---

## 2026-08-10 — Detection sits behind an interface because ultralytics is AGPL-3.0

**Decision.** `Detector` is a Protocol with an `UltralyticsDetector` implementation selected
at runtime. `ultralytics` is imported inside the constructor, never at module scope.

**Why.** `ultralytics` (YOLOv8) is AGPL-3.0. This repository is MIT and the artifact is a
client-facing demo served over a network, which is precisely what the AGPL's network-use
clause reaches. The interface keeps a swap to a permissively-licensed detector (YOLOX,
RT-DETR — both Apache-2.0) a one-file change instead of a rewrite.

**Rules out.** Calling `ultralytics` directly from pipeline, API, or UI code. It also means
the test suite must never require torch — detection is stubbed, so the core suite runs on a
laptop with no inference stack.

**Open.** Whether to ship ultralytics at all is the maintainer's call, not a settled
technical decision. Flagged, not resolved.

---

## 2026-08-10 — ByteTrack imported from its canonical module, supervision pinned <0.31

**Decision.** Tracking uses `supervision.tracker.byte_tracker.core.ByteTrack`, wrapped in a
local `VehicleTracker` Protocol. `supervision` is pinned `>=0.30,<0.31`.

**Why.** The top-level `sv.ByteTrack` export is deprecated as of supervision 0.28 and is
scheduled for removal in 0.31 — verified by the FutureWarning the package emits. The
canonical module path still works, and the pin makes the upgrade a deliberate act rather
than something a `poetry update` does silently.

**Rules out.** Using the top-level alias anywhere. When 0.31 lands, the replacement is a
`VehicleTracker` implementation swap, not a change spread through the pipeline.

---

## 2026-08-10 — One domain, path-prefix routing for the API

**Decision.** Traefik serves the UI at `${DOMAIN}/` and the worker at `${DOMAIN}/api`, with
an explicit router priority so the `/api` rule wins. Rejected: `api.${DOMAIN}` as a separate
subdomain.

**Why.** One DNS A record, one certificate, one Let's Encrypt challenge, and no CORS
preflight — the browser fetches the MJPEG stream same-origin. Materially less to set up and
fewer ways for a demo to fail on someone else's network.

**Rules out.** Serving the API on its own hostname without revisiting `api_public_url` and
the CORS posture, which is currently "not needed because same-origin."

---

## 2026-08-10 — Redis holds ephemeral state with a TTL, and is not a database

**Decision.** Every key the worker writes carries a TTL. Persistence is off (`--save ""`,
`--appendonly no`) with an LRU memory cap.

**Why.** The dashboard must be able to distinguish "live", "stale", and "no data". If state
outlived the process that produced it, a dead worker would leave a dashboard that looks
current — the worst of the three states, because it is silently wrong.

**Rules out.** Using this Redis for anything needing durability: historical queries, replay,
audit, or counts that must survive a restart. Counters reset when the worker restarts, by
design. A real time-series store is a separate decision if that is ever needed.

---

## 2026-08-10 — No plate is ever fabricated; ANPR is a disabled seam

**Decision.** `CrossingEvent.plate_text` is `None` unless a real plate model read it. A
`PlateReader` Protocol and `NullPlateReader` exist; `anpr_enabled` without a configured model
is a startup error, not a silent fallback. The UI renders `—` and says the stage is off.

**Why.** The previous demo generated random plate strings, and the README described an OCR
capability that did not exist. Reintroducing invented plates into a build whose entire point
is that the numbers are real would recreate exactly the credibility problem being fixed.
Bengali-script plates on Bangladeshi vehicles are also not something generic OCR reads, so a
half-working stage would misrepresent capability in front of a client.

**Rules out.** Any placeholder, sample, or "representative" plate value in code, fixtures, or
UI. `None` means not read — never unreadable, never a stand-in.

---

## 2026-08-10 — No lockfile; dependencies resolve from `pyproject.toml`

**Decision.** Deleted `poetry.lock` and `requirements.txt`. Docker images, CI, and
`make install` all resolve with `pip` from `pyproject.toml` and its `ui` / `worker` extras.

**Why.** The committed lock described the February 2024 dependency set and would have made
`poetry install` fail outright. Regenerating it was attempted and abandoned after ~29
minutes: Poetry 1.6 resolving torch's graph did not converge. The images never used the
lock for resolution — they `pip install ".[extra]"` — and with the extras split, one lock
serving two differently-shaped images buys little. `requirements.txt` was referenced by
nothing and pinned `streamlit==1.31.1`, so installing from it would have silently rebuilt
the old world.

**Rules out.** Byte-reproducible installs. Version floors are caret constraints in
`pyproject.toml`, so a fresh install can pick up a new minor release. This is the real cost
of the decision and it is accepted for a demo, not endorsed for anything load-bearing.
Restoring reproducibility means generating a lock on a machine where resolution completes
(a newer Poetry, or `uv lock`) and re-adding it to the image build context — a good
follow-up, not a blocker.

---

## 2026-08-10 — torchvision is the default detector; ultralytics becomes an explicit opt-in

**Decision.** Added `TorchvisionDetector` (`src/traffic_ai/worker/detection.py`), backed by
`torchvision.models.detection.fasterrcnn_mobilenet_v3_large_fpn` with its bundled
COCO-pretrained weights. `Settings.detector` (`TRAFFIC_AI_DETECTOR`) selects the backend and
defaults to `"torchvision"`; `"ultralytics"` remains fully supported, selecting
`UltralyticsDetector` as before. `build_detector` falls back to `StubDetector` with a loud
warning for either value if that backend's library is not importable. This supersedes half
of "Detection sits behind an interface because ultralytics is AGPL-3.0" above: the interface
was built so a swap would be a one-file change, and this is that swap, landing as a default
rather than a replacement — `ultralytics` was not removed.

**Why.** `torchvision` is BSD-3-Clause; `ultralytics` (YOLOv8) is AGPL-3.0 and this
repository is MIT, served over a network, which is exactly what the AGPL's network-use
clause reaches. Making the permissive detector the default means the default deployment
path carries no AGPL obligation, closing the "Open" item left by the interface decision.

Model choice within `torchvision.models.detection` was empirical, not assumed. Benchmarked
`ssdlite320_mobilenet_v3_large` and `fasterrcnn_mobilenet_v3_large_fpn` against
`data/videos/toll-plaza-a.mp4` inside `traffic-ai-worker:test` (CPU, `torch.set_num_threads(1)`,
10 frames sampled across the clip): ssdlite averaged ~0.6-1.2s/frame versus fasterrcnn's
~1.4-4.5s/frame (both single-threaded; absolute numbers varied run to run under host load,
the relative gap — fasterrcnn roughly 3-5x slower — held throughout), but ssdlite scored
real vehicles in this footage at ~0.2-0.3 confidence, almost never clearing the default 0.35
threshold, while fasterrcnn scored the same vehicles at 0.7-0.99 in nearly every sampled
frame. A detector that does not detect the traffic defeats the point of the 2026-08-10
"random to real" rewrite regardless of speed, so fasterrcnn was chosen despite being the
slower model. `torch.set_num_threads(1)` is set in `TorchvisionDetector.__init__` because
two `CameraPipeline`s call `detect()` concurrently (each via `asyncio.to_thread`) on a
small CPU-only server; left at torch's default, one call claims every core and the two
thrash each other instead of timesharing.

**Rules out.** Treating `TRAFFIC_AI_MODEL_WEIGHTS` as backend-agnostic — it is
ultralytics-specific (a bundled name or a path) and `build_detector` only reads it in the
`ultralytics` branch; `TorchvisionDetector` selects its model by its own weights enum and
never sees that setting. Also rules out assuming fasterrcnn's latency is free: on a small
CPU server this is materially slower than ssdlite would have been, and the accepted
mitigation is the existing tuning knobs (`TRAFFIC_AI_DETECT_EVERY_N_FRAMES`,
`TRAFFIC_AI_FRAME_WIDTH`), not a different model, unless a future session benchmarks a
faster torchvision detector that still clears the confidence bar on real footage.

**Measured cost against the previous default.** The comparison that matters is not
ssdlite-vs-fasterrcnn but torchvision-vs-ultralytics, since `ultralytics` was the default
until this entry. Both were run through the real pipeline over the same footage in the
same image:

| Backend | Frames processed | Crossings counted | Window |
| --- | --- | --- | --- |
| `ultralytics` (yolov8n) | 32 → 346 (~7 fps) | 5 | 45s |
| `torchvision` (fasterrcnn) | 38 → 137 (~1.5 fps) | 1 | 67s |

Both runs were on a contended macOS Docker VM, so the absolute figures are soft and the
two windows differ; the direction and rough magnitude are not in doubt — the permissive
default is several times slower and counts visibly fewer vehicles per unit time. An
attempted controlled head-to-head on an idle host did not finish: 12 frames through
fasterrcnn exceeded a 10-minute budget, which is itself a datum.

**This is a live tradeoff, not a closed one.** The default is set for licensing safety, and
the demo-quality cost is real and measured. If the demo looks sluggish in front of a client,
the options in order are: raise `TRAFFIC_AI_DETECT_EVERY_N_FRAMES`, lower
`TRAFFIC_AI_FRAME_WIDTH`, or set `TRAFFIC_AI_DETECTOR=ultralytics` and accept the AGPL
obligation deliberately. Whether a commercial client demo should ship AGPL detection is the
maintainer's call and is deliberately left open here.
