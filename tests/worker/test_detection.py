"""`StubDetector` and `build_detector` — torch-free, must run without ultralytics."""

from __future__ import annotations

import numpy as np
import pytest

from traffic_ai.domain import VEHICLE_CLASSES
from traffic_ai.worker.detection import StubDetector, build_detector


class TestStubDetector:
    def test_is_ready_and_class_names_cover_vehicle_classes(self) -> None:
        detector = StubDetector(boxes_per_frame=3)
        assert detector.is_ready is True
        assert set(detector.class_names.values()) == set(VEHICLE_CLASSES)

    def test_produces_requested_box_count_within_frame_bounds(self) -> None:
        detector = StubDetector(boxes_per_frame=4)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)

        detections = detector.detect(frame)

        assert len(detections) == 4
        assert (detections.xyxy[:, 0] >= 0).all()
        assert (detections.xyxy[:, 2] <= 640).all()
        assert (detections.xyxy[:, 1] >= 0).all()
        assert (detections.xyxy[:, 3] <= 480).all()

    def test_boxes_drift_between_calls(self) -> None:
        detector = StubDetector(boxes_per_frame=1, drift=10.0)
        frame = np.zeros((100, 200, 3), dtype=np.uint8)

        first = detector.detect(frame)
        second = detector.detect(frame)

        assert not np.array_equal(first.xyxy, second.xyxy)

    def test_deterministic_given_the_same_call_sequence(self) -> None:
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        a = StubDetector(boxes_per_frame=2, drift=5.0)
        b = StubDetector(boxes_per_frame=2, drift=5.0)

        for _ in range(5):
            np.testing.assert_array_equal(a.detect(frame).xyxy, b.detect(frame).xyxy)


def test_build_detector_falls_back_to_stub_without_ultralytics(settings) -> None:
    try:
        import ultralytics  # noqa: F401

        pytest.skip("ultralytics is installed in this environment")
    except ImportError:
        pass

    detector = build_detector(settings)

    assert isinstance(detector, StubDetector)
    assert detector.is_ready is True
