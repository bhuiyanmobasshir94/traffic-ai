"""Frame annotation and JPEG encoding.

The only output this module ever produces is encoded JPEG bytes. Nothing here
returns, logs, or stores a raw frame — `pipeline.py` publishes exactly what
`render()` returns, and only that, to `StateStore.publish_frame`.
"""

from __future__ import annotations

import cv2
import numpy as np
import supervision as sv

from traffic_ai.cameras import CountingLine
from traffic_ai.domain import CameraState

_LINE_COLOR = (0, 255, 255)  # BGR yellow
_TEXT_COLOR = (255, 255, 255)
_ERROR_COLOR = (0, 0, 255)


class FrameAnnotator:
    """Draws tracked boxes, the counting line, and a status overlay, then encodes
    the result as JPEG."""

    def __init__(self, line: CountingLine, jpeg_quality: int = 75) -> None:
        self._line = line
        self._jpeg_quality = jpeg_quality
        self._box_annotator = sv.BoxAnnotator()
        self._label_annotator = sv.LabelAnnotator()

    def render(
        self,
        frame: np.ndarray,
        tracked: sv.Detections,
        class_names: dict[int, str],
        state: CameraState,
    ) -> bytes:
        canvas = frame.copy()
        height, width = canvas.shape[:2]

        canvas = self._box_annotator.annotate(canvas, tracked)
        labels = self._labels(tracked, class_names)
        if labels:
            canvas = self._label_annotator.annotate(canvas, tracked, labels=labels)

        start = (round(self._line.start[0] * width), round(self._line.start[1] * height))
        end = (round(self._line.end[0] * width), round(self._line.end[1] * height))
        cv2.line(canvas, start, end, _LINE_COLOR, 2, lineType=cv2.LINE_AA)

        overlay = (
            f"{state.name} | {state.status.value} | {state.congestion.label} | "
            f"{state.throughput_per_min:.1f}/min | tracks={state.active_tracks}"
        )
        cv2.putText(
            canvas, overlay, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, _TEXT_COLOR, 2, cv2.LINE_AA
        )
        if state.error:
            cv2.putText(
                canvas,
                f"ERROR: {state.error}",
                (10, 50),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                _ERROR_COLOR,
                2,
                cv2.LINE_AA,
            )

        ok, buffer = cv2.imencode(
            ".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality]
        )
        if not ok:
            raise RuntimeError("JPEG encode failed")
        return buffer.tobytes()

    @staticmethod
    def _labels(tracked: sv.Detections, class_names: dict[int, str]) -> list[str]:
        if tracked.tracker_id is None or tracked.class_id is None:
            return []
        return [
            f"#{int(tracker_id)} {class_names.get(int(class_id), '?')}"
            for tracker_id, class_id in zip(tracked.tracker_id, tracked.class_id, strict=True)
        ]
