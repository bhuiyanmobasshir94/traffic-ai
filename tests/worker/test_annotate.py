"""`FrameAnnotator` — must always return JPEG bytes, never a raw frame."""

from __future__ import annotations

from datetime import UTC, datetime

import cv2
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


# --- an uncalibrated camera: no congestion, no rate, no counting line -----------------


def test_overlay_for_a_counting_camera_shows_congestion_and_rate() -> None:
    state = _state(congestion=CongestionLevel.HEAVY, throughput_per_min=42.0, active_tracks=7)

    assert FrameAnnotator.overlay_text(state) == (
        "Toll Plaza A | running | Heavy | 42.0/min | tracks=7"
    )


def test_overlay_for_an_uncalibrated_camera_omits_congestion_and_rate() -> None:
    # Values that WOULD be shown if the overlay trusted them: the point is that it does not.
    state = _state(
        name="Toll Plaza B",
        counting_enabled=False,
        congestion=CongestionLevel.STANDSTILL,
        throughput_per_min=42.0,
        active_tracks=7,
    )

    text = FrameAnnotator.overlay_text(state)

    assert text == "Toll Plaza B | running | counting not calibrated | tracks=7"
    assert "Standstill" not in text
    assert "/min" not in text


def _pixel_on_the_counting_line(jpeg: bytes) -> tuple[int, int, int]:
    """BGR of the decoded pixel mid-way along the default line (y=55% of a 120px frame),
    well clear of the text overlay at the top-left."""
    decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    blue, green, red = (int(v) for v in decoded[round(0.55 * 120), 80])
    return blue, green, red


def test_counting_line_is_drawn_only_for_a_camera_that_counts() -> None:
    annotator = FrameAnnotator(CountingLine(), jpeg_quality=95)
    frame = np.zeros((120, 160, 3), dtype=np.uint8)

    counting = _pixel_on_the_counting_line(
        annotator.render(frame, sv.Detections.empty(), {}, _state())
    )
    uncalibrated = _pixel_on_the_counting_line(
        annotator.render(frame, sv.Detections.empty(), {}, _state(counting_enabled=False))
    )

    # The yellow line (BGR 0,255,255) is on the frame for the counting camera...
    assert counting[1] > 150 and counting[2] > 150
    # ...and the frame is untouched at that spot for the uncalibrated one.
    assert max(uncalibrated) < 40


def test_render_never_mutates_the_source_frame() -> None:
    annotator = FrameAnnotator(CountingLine(), jpeg_quality=75)
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    original = frame.copy()

    annotator.render(frame, sv.Detections.empty(), {}, _state())

    np.testing.assert_array_equal(frame, original)
