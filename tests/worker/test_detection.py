"""`StubDetector` and `build_detector` — torch-free, must run without ultralytics."""

from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest
import structlog.testing
from pydantic import ValidationError

from traffic_ai.config import Settings
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

    detector_settings = settings.model_copy(update={"detector": "ultralytics"})
    with structlog.testing.capture_logs() as logs:
        detector = build_detector(detector_settings)

    assert isinstance(detector, StubDetector)
    assert detector.is_ready is True
    assert any(
        entry["log_level"] == "warning" and "ultralytics" in entry["event"] for entry in logs
    )


def test_build_detector_falls_back_to_stub_without_torchvision(settings) -> None:
    try:
        import torch  # noqa: F401
        import torchvision  # noqa: F401

        pytest.skip("torch/torchvision is installed in this environment")
    except ImportError:
        pass

    # `settings` already defaults to `detector="torchvision"`, but set it
    # explicitly so this test's intent survives a future default change.
    detector_settings = settings.model_copy(update={"detector": "torchvision"})
    with structlog.testing.capture_logs() as logs:
        detector = build_detector(detector_settings)

    assert isinstance(detector, StubDetector)
    assert detector.is_ready is True
    assert any(
        entry["log_level"] == "warning" and "torchvision" in entry["event"] for entry in logs
    )


def test_importing_detection_module_imports_no_inference_library() -> None:
    """`traffic_ai.worker.detection` must be importable with none of `torch`,
    `torchvision`, or `ultralytics` in `sys.modules` — both real detectors
    import their library lazily, inside `__init__`. Checked in a fresh
    interpreter, mirroring `tests/api/test_app.py`'s worker-isolation check.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import traffic_ai.worker.detection, sys; "
            "assert not [m for m in sys.modules "
            "if m.split('.')[0] in {'torch', 'torchvision', 'ultralytics'}]",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_settings_rejects_invalid_detector_value() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, detector="not-a-real-detector")  # type: ignore[call-arg]


@pytest.mark.requires_inference
class TestTorchvisionDetector:
    def test_detects_nothing_above_threshold_on_a_blank_frame(self, settings) -> None:
        from traffic_ai.worker.detection import TorchvisionDetector

        detector = TorchvisionDetector(
            device=settings.device,
            confidence=settings.confidence_threshold,
            iou=settings.iou_threshold,
        )
        frame = np.zeros((480, 640, 3), dtype=np.uint8)

        detections = detector.detect(frame)

        assert detector.is_ready is True
        assert set(VEHICLE_CLASSES) <= set(detector.class_names.values())
        # A blank frame must not break the zero-detection array shapes that
        # `tracking.py` and `counting.py` expect from `sv.Detections.empty()`.
        assert len(detections) == 0
        assert detections.xyxy.shape == (0, 4)

    def test_build_detector_returns_torchvision_detector_by_default(self, settings) -> None:
        from traffic_ai.worker.detection import TorchvisionDetector

        detector = build_detector(settings)

        assert isinstance(detector, TorchvisionDetector)
