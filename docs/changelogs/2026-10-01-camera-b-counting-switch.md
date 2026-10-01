# 2026-10-01 — Stop camera B presenting fabricated counts

## What
- `cameras.py`: `CameraConfig.counting_enabled` / `counting_disabled_reason`; Toll Plaza B off.
- `domain.py`: additive `CameraState.counting_enabled` (default `True`).
- `worker/pipeline.py`, `worker/annotate.py`: a disabled camera detects, tracks, annotates and
  streams, but never counts, records crossings, writes history, or derives throughput or
  congestion. The overlay reads "counting not calibrated"; no counting line is drawn.
- API: 409 with the reason for `/api/history/*?camera_id=<disabled>` and
  `/api/cameras/<disabled>/events`; `/api/events` skips disabled cameras. State, frame and
  stream stay 200. Order: 404 unknown → 409 not calibrated → 422 → 503.
- UI: dashboard and Analytics show the reason instead of metrics; the map draws the camera
  grey; the client maps 409 to "not calibrated" (never "unreachable").
- README capability rows and DEPLOYMENT.md updated; decision-log entry added.

## Why
The user asked to fix camera B's counting line. Measurement showed no line can work: 900
frames gave 3,932 track IDs for ~50 vehicles, and lines at y=0.40–0.95 gave 0–540 crossings
for the same 30 s against ~20–30 real incoming vehicles (slit-scan). Its numbers were
fabricated measurements, which the project forbids.

## How
Contain, don't guess: a per-camera switch rather than a tuned line. Reviewer pass found no
critical issues and three warnings; the API gate and the "All cameras" caption were fixed, and
the state-JSON default values were accepted and recorded in DECISIONS.md.

## Intended outcome
Camera B shows live video and active tracks, and nowhere shows a count, rate, congestion, or
history. Signal it worked: B's overlay and dashboard say "not calibrated", its history routes
return 409, and camera A still counts.

## Verification
- `pytest -o addopts=""` → 991 passed, 24 skipped; `ruff check .` / `ruff format --check .` clean.
- Local stack rebuilt and run: B state `counting_enabled: false`, 0 counted; A counting;
  B overlay "Toll Plaza B | running | counting not calibrated | tracks=47", no line drawn;
  B history and events → 409 with the reason; A history → 200; unknown camera → 404; B state → 200.
- Implementer mutation checks: 28 mutants across both passes, all killed.

## Critical notes
- `/api/history/*` with no `camera_id` is not gated. Camera-B rows written before this change
  would still appear in "All cameras" totals; the local DB has none (all 64 rows are camera A).
- B's state JSON carries model defaults (`free_flow`, zero counts) beside the flag. Only safe
  while the bundled UI is the sole consumer; roll the UI out with or before the worker.
- `pages/Traffic_Analysis.py` defaults to camera B, so that page now opens on the
  "not calibrated" notice.
- Follow-up: fix tracking on B (tracker tuning, another detector, or other footage), validate
  against a hand count, then set `counting_enabled=True`.
