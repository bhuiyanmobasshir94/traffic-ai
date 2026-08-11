---
name: traffic-ai-worker-package
description: Structure and test seam of src/traffic_ai/worker/ — the detect/track/count/annotate pipeline, CameraPipeline._process_frame as the single-tick test entry point.
metadata:
  type: project
---

`src/traffic_ai/worker/` (`detection.py`, `tracking.py`, `counting.py`, `annotate.py`,
`anpr.py`, `pipeline.py`) implements the inference pipeline behind swappable `Protocol`
interfaces (`Detector`, `VehicleTracker`, `PlateReader`). `pipeline.CameraPipeline` is the
composition root; `run()` is the long-running, never-raises video loop, but there is no
video file in this repo (non-goal: don't download one) and no public "single tick" method
in the contracted interface.

**The seam:** `CameraPipeline._process_frame(frame: np.ndarray) -> None` is a private
(underscore-prefixed, not part of the contracted interface) async method that runs exactly
one tick — detect/track/count/annotate/publish — against an already-decoded frame. Tests
build a synthetic `np.zeros((H, W, 3), uint8)` frame and call this directly with the
`store` fakeredis fixture from `tests/conftest.py`, and `detector=StubDetector()`,
`tracker=ByteTrackTracker()` injected via the constructor's DI hooks. This is how "a
CameraPipeline tick against StubDetector + fakeredis producing a published CameraState" is
tested without any real video I/O — see `tests/worker/test_pipeline.py`.

Detection cadence: `_frame_index` starts at 0 and detection runs when
`_frame_index % detect_every_n_frames == 0`, checked *before* incrementing — so the very
first tick always detects, which keeps single-tick tests deterministic regardless of the
configured `detect_every_n_frames`.

`LineCounter`'s incoming/outgoing side is resolved once at construction into a single
boolean (`_positive_cross_is_incoming`) via a cross-product-sign derivation tied to the
line's `start`->`end` x-delta sign XNOR'd with `incoming_is_downward` — see the docstring
in `counting.py` for the full derivation if this needs revisiting for a non-horizontal
line.

`build_plate_reader` (`anpr.py`) returns `NullPlateReader()` in every valid configuration,
including `anpr_enabled=True` with a model path set — no model-backed `PlateReader`
implementation ships with this project yet, so a configured-but-unbuilt model path is not
an error, just still a no-op reader.
