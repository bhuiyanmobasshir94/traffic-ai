"""`FrameAnnotator` — must always return JPEG bytes, never a raw frame."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import supervision as sv

from traffic_ai.cameras import CountingLine
from traffic_ai.domain import CameraState, CongestionLevel, PipelineStatus
from traffic_ai.worker.annotate import FrameAnnotator

_JPEG_MAGIC = b"\xff\xd8"


def _state(**overrides) -> CameraState:
    defaults = {
        "camera_id": "toll-plaza-a",
        "name": "Toll Plaza A",
        "status": PipelineStatus.RUNNING,
        "updated_at": datetime.now(UTC),
        "congestion": CongestionLevel.FREE_FLOW,
    }
    defaults.update(overrides)
    return CameraState(**defaults)


def test_render_returns_jpeg_bytes_for_empty_detections() -> None:
    annotator = FrameAnnotator(CountingLine(), jpeg_quality=75)
    frame = np.zeros((120, 160, 3), dtype=np.uint8)

    jpeg = annotator.render(frame, sv.Detections.empty(), {}, _state())

    assert isinstance(jpeg, bytes)
    assert jpeg[:2] == _JPEG_MAGIC


def test_render_returns_jpeg_bytes_with_tracked_detections() -> None:
    annotator = FrameAnnotator(CountingLine(), jpeg_quality=75)
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    detections = sv.Detections(
        xyxy=np.array([[10.0, 10.0, 40.0, 40.0]], dtype=np.float32),
        confidence=np.array([0.9], dtype=np.float32),
        class_id=np.array([0]),
        tracker_id=np.array([1]),
    )

    jpeg = annotator.render(frame, detections, {0: "car"}, _state())

    assert jpeg[:2] == _JPEG_MAGIC


def test_render_does_not_raise_when_state_has_an_error() -> None:
    annotator = FrameAnnotator(CountingLine(), jpeg_quality=75)
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    errored = _state(status=PipelineStatus.ERROR, error="could not open video")

    jpeg = annotator.render(frame, sv.Detections.empty(), {}, errored)

    assert jpeg[:2] == _JPEG_MAGIC


def test_render_never_mutates_the_source_frame() -> None:
    annotator = FrameAnnotator(CountingLine(), jpeg_quality=75)
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    original = frame.copy()

    annotator.render(frame, sv.Detections.empty(), {}, _state())

    np.testing.assert_array_equal(frame, original)
