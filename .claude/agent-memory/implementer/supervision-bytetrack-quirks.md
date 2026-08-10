---
name: supervision-bytetrack-quirks
description: supervision==0.30.0 behavior for ByteTrack — canonical-path import still warns, reset() fully restarts id assignment from 1.
metadata:
  type: project
---

Verified against the installed `supervision==0.30.0` in `traffic-ai`'s `.venv`:

- `supervision.tracker.byte_tracker.core.ByteTrack` — the "canonical" module path the
  project's decision log mandates over the deprecated top-level `sv.ByteTrack` — is itself
  wrapped in a `@deprecated_class(target=TargetMode.NOTIFY, ...)` decorator in this exact
  pinned version. Both import paths currently resolve to the same class and both emit the
  same `FutureWarning` on construction. `TargetMode.NOTIFY` only warns; it does not redirect
  calls, so the tracker is fully functional despite the warning.
- `ByteTrack.reset()` fully clears internal id assignment — the next track created after
  `reset()` gets id `1` again, not a continuation of the previous sequence. This is what
  makes it safe to call after a video-loop discontinuity (frame N back to frame 0).
- `sv.Detections.get_anchors_coordinates(sv.Position.BOTTOM_CENTER)` returns an (N, 2)
  array; `sv.BoxAnnotator()` / `sv.LabelAnnotator()` handle zero-length `Detections`
  (`sv.Detections.empty()`) as a no-op with no special-casing needed by callers.

**Why:** confirmed by direct interactive testing, not from supervision's docs (which
describe the target API, `trackers.ByteTrackTracker`, not this pinned version).

**How to apply:** don't try to silence the FutureWarning or treat it as a bug to fix — it's
expected in this pinned version and doesn't affect correctness. See
`docs/decisions/DECISIONS.md` (2026-08-10, "ByteTrack imported from its canonical module")
for why the import path is still a project rule regardless of the warning.
