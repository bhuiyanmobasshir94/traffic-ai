"""Line-crossing counts, throughput measurement, and congestion derivation.

Three independent pieces that `pipeline.py` composes each tick:

- `LineCounter` — edge-triggered crossing detection against a camera's configured
  `CountingLine`.
- `ThroughputWindow` — a rolling count of crossings, converted to a per-minute rate.
- `derive_congestion` — the traffic-flow fundamental (density x flow), not a raw
  vehicle count.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import supervision as sv

from traffic_ai.cameras import CountingLine
from traffic_ai.domain import ClassCounts, CongestionLevel, Direction
from traffic_ai.store import utcnow


@dataclass(frozen=True)
class Crossing:
    """One track observed crossing the counting line on this tick."""

    track_id: int
    vehicle_class: str
    direction: Direction
    confidence: float


class LineCounter:
    """Counts tracked vehicles crossing a camera's configured `CountingLine`.

    Crossings are edge-triggered: a track is counted the tick its anchor point
    (bottom-center of its box) moves to the opposite side of the line from where
    it was last observed — never once per frame it merely remains on one side. A
    track's first observation only establishes its starting side; it cannot
    register a crossing until a later, different-side observation confirms it
    actually moved across the line. This is what keeps a track that sits near the
    line, or that is only ever seen on one side, from being double-counted.

    `incoming_is_downward` is resolved once, at construction, into a fixed
    relationship between the sign of the line's cross product and "incoming" —
    see `_is_incoming_side` for the derivation.
    """

    def __init__(self, line: CountingLine, frame_width: int, frame_height: int) -> None:
        self._start = np.array(
            [line.start[0] * frame_width, line.start[1] * frame_height], dtype=np.float64
        )
        self._end = np.array(
            [line.end[0] * frame_width, line.end[1] * frame_height], dtype=np.float64
        )
        line_dx = self._end[0] - self._start[0]
        # For a directed line start->end, the cross product of (end-start) with
        # (point-start) is positive on one side and negative on the other. Moving
        # straight down (image y increasing) by one unit from a point ON the line
        # changes that cross product by exactly `line_dx` (the y-term drops out).
        # So `line_dx >= 0` tells us which sign of the cross product is the
        # "downward" side; XNOR-ing that with `incoming_is_downward` collapses
        # both into one fixed boolean we can check per point without re-deriving
        # the geometry on every call.
        self._positive_cross_is_incoming = (line_dx >= 0) == line.incoming_is_downward
        self._track_side: dict[int, bool] = {}
        self._counts: dict[Direction, ClassCounts] = {d: ClassCounts() for d in Direction}

    def _is_incoming_side(self, point: np.ndarray) -> bool:
        line_vec = self._end - self._start
        point_vec = point - self._start
        cross = line_vec[0] * point_vec[1] - line_vec[1] * point_vec[0]
        return bool((cross > 0) == self._positive_cross_is_incoming)

    def update(self, tracked: sv.Detections, class_names: dict[int, str]) -> list[Crossing]:
        crossings: list[Crossing] = []
        if len(tracked) == 0 or tracked.tracker_id is None:
            return crossings

        anchors = tracked.get_anchors_coordinates(sv.Position.BOTTOM_CENTER)
        for i, raw_track_id in enumerate(tracked.tracker_id):
            track_id = int(raw_track_id)
            side = self._is_incoming_side(anchors[i])
            previous_side = self._track_side.get(track_id)
            self._track_side[track_id] = side

            if previous_side is None or previous_side == side:
                continue  # first sighting, or still on the same side: no crossing

            class_id = int(tracked.class_id[i]) if tracked.class_id is not None else None
            vehicle_class = (
                class_names.get(class_id, "unknown") if class_id is not None else "unknown"
            )
            confidence = float(tracked.confidence[i]) if tracked.confidence is not None else 0.0
            direction = Direction.INCOMING if side else Direction.OUTGOING

            self._counts[direction].increment(vehicle_class)
            crossings.append(
                Crossing(
                    track_id=track_id,
                    vehicle_class=vehicle_class,
                    direction=direction,
                    confidence=confidence,
                )
            )
        return crossings

    @property
    def counts(self) -> dict[Direction, ClassCounts]:
        """A snapshot of counts so far. Copied out so callers embedding this in a
        `CameraState` do not end up holding a reference that mutates under them."""
        return {direction: counts.model_copy() for direction, counts in self._counts.items()}


class ThroughputWindow:
    """A rolling count of crossings over `window_seconds`, exposed as a per-minute
    rate. `now` is injectable on every call so tests can simulate elapsed time
    without a real sleep; production callers rely on the default (`store.utcnow`).
    """

    def __init__(self, window_seconds: float = 60.0) -> None:
        self._window_seconds = window_seconds
        self._events: deque[datetime] = deque()

    def record(self, n: int = 1, *, now: datetime | None = None) -> None:
        moment = now if now is not None else utcnow()
        for _ in range(n):
            self._events.append(moment)
        self._evict(moment)

    def per_minute(self, *, now: datetime | None = None) -> float:
        moment = now if now is not None else utcnow()
        self._evict(moment)
        if self._window_seconds <= 0:
            return 0.0
        return len(self._events) * (60.0 / self._window_seconds)

    def _evict(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._window_seconds)
        while self._events and self._events[0] < cutoff:
            self._events.popleft()


def derive_congestion(
    *,
    throughput_per_min: float,
    capacity_per_min: float,
    active_tracks: int,
    density_saturation: int = 25,
) -> CongestionLevel:
    """Congestion is density-with-low-flow, not "many vehicles". A corridor packed
    with vehicles that are still moving well is MODERATE, not HEAVY; a corridor
    that is merely busy but flowing freely is FREE_FLOW.

    Division is guarded defensively on both terms even though `Settings` already
    validates `capacity_per_min > 0` — `density_saturation` is a plain function
    argument with no such guarantee.
    """
    density = min(1.0, active_tracks / density_saturation) if density_saturation > 0 else 0.0
    flow = min(1.0, throughput_per_min / capacity_per_min) if capacity_per_min > 0 else 0.0

    if density < 0.25:
        return CongestionLevel.FREE_FLOW
    if density < 0.55:
        return CongestionLevel.MODERATE if flow < 0.5 else CongestionLevel.FREE_FLOW
    if flow < 0.15:
        return CongestionLevel.STANDSTILL
    if flow < 0.50:
        return CongestionLevel.HEAVY
    return CongestionLevel.MODERATE
