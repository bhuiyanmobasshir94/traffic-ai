"""Multi-object tracking behind a swappable interface.

Wraps supervision's ByteTrack implementation. Imported from its canonical module
path, `supervision.tracker.byte_tracker.core`, rather than the top-level `sv.ByteTrack`
re-export — that alias is deprecated as of supervision 0.28 and scheduled for removal
in 0.31 (this project pins `supervision>=0.30,<0.31`). See `docs/decisions/DECISIONS.md`
(2026-08-10, "ByteTrack imported from its canonical module"). Both paths currently
resolve to the same class and both emit the library's own `FutureWarning` on
construction in this pinned version; only the import path is a project rule.
"""

from __future__ import annotations

from typing import Protocol

import supervision as sv
from supervision.tracker.byte_tracker.core import ByteTrack


class VehicleTracker(Protocol):
    def update(self, detections: sv.Detections) -> sv.Detections: ...

    def reset(self) -> None: ...


class ByteTrackTracker:
    """Assigns stable `tracker_id`s to detections across frames.

    `reset()` must be called whenever track identity cannot be trusted to persist
    — e.g. after the source video loops back to frame 0 (`pipeline.py`) — since a
    track id surviving a discontinuity would silently merge two unrelated vehicles.
    """

    def __init__(
        self,
        frame_rate: float = 12.0,
        track_activation_threshold: float = 0.25,
        lost_track_buffer: int = 30,
        minimum_matching_threshold: float = 0.8,
    ) -> None:
        self._frame_rate = frame_rate
        self._track_activation_threshold = track_activation_threshold
        self._lost_track_buffer = lost_track_buffer
        self._minimum_matching_threshold = minimum_matching_threshold
        self._tracker = self._new_tracker()

    def _new_tracker(self) -> ByteTrack:
        return ByteTrack(
            track_activation_threshold=self._track_activation_threshold,
            lost_track_buffer=self._lost_track_buffer,
            minimum_matching_threshold=self._minimum_matching_threshold,
            frame_rate=self._frame_rate,
        )

    def update(self, detections: sv.Detections) -> sv.Detections:
        return self._tracker.update_with_detections(detections)

    def reset(self) -> None:
        self._tracker.reset()
