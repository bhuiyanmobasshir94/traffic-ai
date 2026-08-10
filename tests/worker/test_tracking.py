"""`ByteTrackTracker` — must wrap supervision's canonical `ByteTrack` module path."""

from __future__ import annotations

import numpy as np
import supervision as sv
from supervision.tracker.byte_tracker.core import ByteTrack

from traffic_ai.worker import tracking
from traffic_ai.worker.tracking import ByteTrackTracker


def _detections(boxes: list[tuple[float, float, float, float]]) -> sv.Detections:
    xyxy = np.array(boxes, dtype=np.float32)
    return sv.Detections(
        xyxy=xyxy,
        confidence=np.full(len(boxes), 0.9, dtype=np.float32),
        class_id=np.zeros(len(boxes), dtype=int),
    )


def test_imports_bytetrack_from_its_canonical_module_path() -> None:
    """Guards against a regression to the deprecated top-level `sv.ByteTrack`
    alias, which is scheduled for removal in supervision 0.31."""
    assert tracking.ByteTrack is ByteTrack


def test_update_assigns_a_tracker_id_per_detection() -> None:
    tracker = ByteTrackTracker(frame_rate=12.0)

    tracked = tracker.update(_detections([(10, 10, 50, 50), (100, 100, 150, 150)]))

    assert tracked.tracker_id is not None
    assert len(tracked.tracker_id) == 2


def test_reset_restarts_track_identity_from_scratch() -> None:
    tracker = ByteTrackTracker(frame_rate=12.0)
    first = tracker.update(_detections([(10, 10, 50, 50)]))
    first_id = int(first.tracker_id[0])

    tracker.reset()

    # Same box, but the tracker must not remember it as the same track — this is
    # what makes it safe to call after the demo video loops back to frame 0,
    # where track identity must not survive the discontinuity.
    second = tracker.update(_detections([(10, 10, 50, 50)]))
    second_id = int(second.tracker_id[0])

    assert second_id == first_id == 1
