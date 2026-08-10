"""Vehicle detection behind a swappable interface.

`ultralytics` (YOLOv8) is AGPL-3.0 and this repository is MIT, served over a
network — exactly what the AGPL's network-use clause reaches. So no module in this
codebase imports `ultralytics` at module scope: `UltralyticsDetector` imports it
lazily, inside `__init__`, which keeps `traffic_ai.worker` importable (and the core
test suite runnable) on a machine that never installs torch. See
`docs/decisions/DECISIONS.md` (2026-08-10, "Detection sits behind an interface").
"""

from __future__ import annotations

from typing import Protocol

import numpy as np
import supervision as sv

from traffic_ai.config import Settings
from traffic_ai.domain import VEHICLE_CLASSES
from traffic_ai.logging import get_logger

log = get_logger(__name__)


class Detector(Protocol):
    """A frame-in, detections-out interface. Every implementation must be able to
    report whether it is ready and which class ids map to which label."""

    def detect(self, frame: np.ndarray) -> sv.Detections: ...

    @property
    def is_ready(self) -> bool: ...

    @property
    def class_names(self) -> dict[int, str]: ...


class UltralyticsDetector:
    """Detector backed by an Ultralytics YOLO model.

    Only ever constructed by `build_detector` after confirming `ultralytics` is
    importable. Detections are pre-filtered to `allowed_classes` here, at the
    source, so downstream stages never see a COCO label outside the vehicle set.
    """

    def __init__(
        self,
        weights: str,
        device: str,
        confidence: float,
        iou: float,
        allowed_classes: tuple[str, ...] = VEHICLE_CLASSES,
    ) -> None:
        # Deliberately lazy — see module docstring. Must never move to module scope.
        from ultralytics import YOLO

        self._model = YOLO(weights)
        self._device = device
        self._confidence = confidence
        self._iou = iou
        self._allowed_classes = set(allowed_classes)

        names = self._model.names
        self._class_names: dict[int, str] = (
            dict(names) if isinstance(names, dict) else dict(enumerate(names))
        )
        self._allowed_class_ids = {
            class_id
            for class_id, name in self._class_names.items()
            if name in self._allowed_classes
        }

    def detect(self, frame: np.ndarray) -> sv.Detections:
        results = self._model.predict(
            frame,
            device=self._device,
            conf=self._confidence,
            iou=self._iou,
            verbose=False,
        )[0]
        detections = sv.Detections.from_ultralytics(results)
        if detections.class_id is not None and self._allowed_class_ids:
            keep = np.isin(detections.class_id, list(self._allowed_class_ids))
            detections = detections[keep]
        return detections

    @property
    def is_ready(self) -> bool:
        return True

    @property
    def class_names(self) -> dict[int, str]:
        return self._class_names


class StubDetector:
    """Deterministic, torch-free detector for tests and for running the stack
    before real weights are downloaded.

    Produces `boxes_per_frame` boxes that drift smoothly left-to-right across the
    frame (wrapping at the edge) so a tracker downstream sees stable, trackable
    motion rather than noise. Nothing here reads any file or network resource.
    """

    def __init__(self, boxes_per_frame: int = 3, drift: float = 6.0) -> None:
        self._boxes_per_frame = boxes_per_frame
        self._drift = drift
        self._tick = 0
        self._class_names: dict[int, str] = dict(enumerate(VEHICLE_CLASSES))

    def detect(self, frame: np.ndarray) -> sv.Detections:
        height, width = frame.shape[:2]
        box_w, box_h = width * 0.08, height * 0.08

        xyxy = np.zeros((self._boxes_per_frame, 4), dtype=np.float32)
        class_id = np.zeros(self._boxes_per_frame, dtype=int)
        confidence = np.full(self._boxes_per_frame, 0.9, dtype=np.float32)

        for i in range(self._boxes_per_frame):
            lane_y = height * (0.2 + 0.6 * (i / max(1, self._boxes_per_frame)))
            cx = (i * width / max(1, self._boxes_per_frame) + self._tick * self._drift) % width
            x1, x2 = max(0.0, cx - box_w / 2), min(float(width), cx + box_w / 2)
            y1, y2 = max(0.0, lane_y - box_h / 2), min(float(height), lane_y + box_h / 2)
            xyxy[i] = (x1, y1, x2, y2)
            class_id[i] = i % len(self._class_names)

        self._tick += 1
        return sv.Detections(xyxy=xyxy, confidence=confidence, class_id=class_id)

    @property
    def is_ready(self) -> bool:
        return True

    @property
    def class_names(self) -> dict[int, str]:
        return self._class_names


def build_detector(settings: Settings) -> Detector:
    """`UltralyticsDetector` when `ultralytics` is importable, else `StubDetector`
    with a loud warning log — the stack still runs end to end, with synthetic
    detections, before real weights are in place."""
    try:
        import ultralytics  # noqa: F401
    except ImportError:
        log.warning(
            "ultralytics_not_installed_using_stub_detector",
            detail="detections are synthetic; install the [worker] extra and set "
            "TRAFFIC_AI_MODEL_WEIGHTS for real inference",
        )
        return StubDetector()

    return UltralyticsDetector(
        weights=settings.model_weights,
        device=settings.device,
        confidence=settings.confidence_threshold,
        iou=settings.iou_threshold,
    )
