"""`LineCounter`, `ThroughputWindow`, and `derive_congestion`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import supervision as sv

from traffic_ai.cameras import CountingLine
from traffic_ai.domain import CongestionLevel, Direction
from traffic_ai.worker.counting import LineCounter, ThroughputWindow, derive_congestion

CLASS_NAMES = {0: "car"}


def _detection(*, y: float, tracker_id: int = 1, class_id: int = 0) -> sv.Detections:
    """A single box whose bottom edge (the counting anchor) sits at `y`."""
    return sv.Detections(
        xyxy=np.array([[40.0, y - 10.0, 60.0, y]], dtype=np.float32),
        confidence=np.array([0.9], dtype=np.float32),
        class_id=np.array([class_id]),
        tracker_id=np.array([tracker_id]),
    )


@pytest.fixture
def line_counter() -> LineCounter:
    # Default CountingLine: horizontal at y=0.55, incoming_is_downward=True, over
    # a 100x100 frame -> incoming means "moving to higher y" (downward).
    return LineCounter(CountingLine(), frame_width=100, frame_height=100)


class TestLineCounter:
    def test_first_sighting_establishes_a_baseline_and_never_counts(
        self, line_counter: LineCounter
    ) -> None:
        crossings = line_counter.update(_detection(y=20), CLASS_NAMES)

        assert crossings == []
        assert line_counter.counts[Direction.INCOMING].total == 0
        assert line_counter.counts[Direction.OUTGOING].total == 0

    def test_moving_downward_counts_as_incoming(self, line_counter: LineCounter) -> None:
        line_counter.update(_detection(y=20), CLASS_NAMES)  # above the line
        crossings = line_counter.update(_detection(y=80), CLASS_NAMES)  # now below

        assert len(crossings) == 1
        assert crossings[0].direction is Direction.INCOMING
        assert crossings[0].vehicle_class == "car"
        assert crossings[0].track_id == 1
        assert line_counter.counts[Direction.INCOMING].car == 1
        assert line_counter.counts[Direction.OUTGOING].total == 0

    def test_moving_upward_counts_as_outgoing(self, line_counter: LineCounter) -> None:
        line_counter.update(_detection(y=80), CLASS_NAMES)  # below the line
        crossings = line_counter.update(_detection(y=20), CLASS_NAMES)  # now above

        assert len(crossings) == 1
        assert crossings[0].direction is Direction.OUTGOING
        assert line_counter.counts[Direction.OUTGOING].car == 1
        assert line_counter.counts[Direction.INCOMING].total == 0

    def test_reversed_camera_flips_which_side_is_incoming(self) -> None:
        line = CountingLine(incoming_is_downward=False)
        counter = LineCounter(line, frame_width=100, frame_height=100)

        counter.update(_detection(y=20), CLASS_NAMES)
        crossings = counter.update(_detection(y=80), CLASS_NAMES)

        assert crossings[0].direction is Direction.OUTGOING

    def test_a_track_lingering_on_one_side_is_never_double_counted(
        self, line_counter: LineCounter
    ) -> None:
        line_counter.update(_detection(y=20), CLASS_NAMES)
        first = line_counter.update(_detection(y=80), CLASS_NAMES)
        assert len(first) == 1

        # The same track stays below the line for several more ticks — none of
        # these are further crossings, just continued presence on one side.
        for y in (81.0, 85.0, 90.0, 79.0):
            again = line_counter.update(_detection(y=y), CLASS_NAMES)
            assert again == []

        assert line_counter.counts[Direction.INCOMING].car == 1

    def test_a_track_crossing_back_and_forth_counts_each_real_crossing(
        self, line_counter: LineCounter
    ) -> None:
        line_counter.update(_detection(y=20), CLASS_NAMES)
        crossed_in = line_counter.update(_detection(y=80), CLASS_NAMES)
        crossed_out = line_counter.update(_detection(y=20), CLASS_NAMES)

        assert crossed_in[0].direction is Direction.INCOMING
        assert crossed_out[0].direction is Direction.OUTGOING
        assert line_counter.counts[Direction.INCOMING].car == 1
        assert line_counter.counts[Direction.OUTGOING].car == 1

    def test_empty_detections_returns_no_crossings(self, line_counter: LineCounter) -> None:
        assert line_counter.update(sv.Detections.empty(), CLASS_NAMES) == []

    def test_detections_without_tracker_id_are_ignored(self, line_counter: LineCounter) -> None:
        untracked = sv.Detections(
            xyxy=np.array([[40.0, 10.0, 60.0, 20.0]], dtype=np.float32),
            confidence=np.array([0.9], dtype=np.float32),
            class_id=np.array([0]),
        )
        assert line_counter.update(untracked, CLASS_NAMES) == []


class TestDeriveCongestion:
    def test_low_density_is_free_flow_regardless_of_flow(self) -> None:
        level = derive_congestion(
            throughput_per_min=100.0,
            capacity_per_min=60.0,
            active_tracks=0,
            density_saturation=25,
        )
        assert level is CongestionLevel.FREE_FLOW

    def test_moderate_density_low_flow_is_moderate(self) -> None:
        # density = 10/25 = 0.4 (in [0.25, 0.55)); flow = 5/60 ~= 0.083 (< 0.5)
        level = derive_congestion(
            throughput_per_min=5.0, capacity_per_min=60.0, active_tracks=10, density_saturation=25
        )
        assert level is CongestionLevel.MODERATE

    def test_moderate_density_high_flow_is_free_flow(self) -> None:
        # density = 0.4; flow = 60/60 = 1.0 (>= 0.5)
        level = derive_congestion(
            throughput_per_min=60.0, capacity_per_min=60.0, active_tracks=10, density_saturation=25
        )
        assert level is CongestionLevel.FREE_FLOW

    def test_density_exactly_at_the_quarter_boundary_is_not_free_flow(self) -> None:
        # density == 0.25 exactly is NOT < 0.25, so it falls into the next branch.
        level = derive_congestion(
            throughput_per_min=0.0, capacity_per_min=60.0, active_tracks=1, density_saturation=4
        )
        assert level is CongestionLevel.MODERATE

    def test_density_exactly_at_the_high_boundary_skips_the_moderate_branch(self) -> None:
        # density == 0.55 exactly is NOT < 0.55, so high flow lands on the final
        # else (MODERATE), not on the density<0.55 branch's FREE_FLOW outcome.
        level = derive_congestion(
            throughput_per_min=60.0, capacity_per_min=60.0, active_tracks=11, density_saturation=20
        )
        assert level is CongestionLevel.MODERATE

    def test_high_density_near_zero_flow_is_standstill(self) -> None:
        # density = 20/25 = 0.8; flow = 1/60 ~= 0.017 (< 0.15)
        level = derive_congestion(
            throughput_per_min=1.0, capacity_per_min=60.0, active_tracks=20, density_saturation=25
        )
        assert level is CongestionLevel.STANDSTILL

    def test_flow_exactly_at_the_standstill_boundary_is_heavy(self) -> None:
        # density = 0.8; flow == 0.15 exactly is NOT < 0.15.
        level = derive_congestion(
            throughput_per_min=9.0, capacity_per_min=60.0, active_tracks=20, density_saturation=25
        )
        assert level is CongestionLevel.HEAVY

    def test_high_density_moderate_flow_is_heavy(self) -> None:
        # density = 0.8; flow = 15/60 = 0.25 (in [0.15, 0.5))
        level = derive_congestion(
            throughput_per_min=15.0, capacity_per_min=60.0, active_tracks=20, density_saturation=25
        )
        assert level is CongestionLevel.HEAVY

    def test_flow_exactly_at_the_heavy_boundary_is_moderate(self) -> None:
        # density = 0.8; flow == 0.50 exactly is NOT < 0.50.
        level = derive_congestion(
            throughput_per_min=30.0, capacity_per_min=60.0, active_tracks=20, density_saturation=25
        )
        assert level is CongestionLevel.MODERATE

    def test_high_density_high_flow_is_moderate(self) -> None:
        # density = 0.8; flow = 45/60 = 0.75 (>= 0.5)
        level = derive_congestion(
            throughput_per_min=45.0, capacity_per_min=60.0, active_tracks=20, density_saturation=25
        )
        assert level is CongestionLevel.MODERATE

    def test_zero_capacity_is_guarded_and_does_not_raise(self) -> None:
        level = derive_congestion(
            throughput_per_min=10.0, capacity_per_min=0.0, active_tracks=0, density_saturation=25
        )
        assert level is CongestionLevel.FREE_FLOW

    def test_zero_density_saturation_is_guarded_and_does_not_raise(self) -> None:
        level = derive_congestion(
            throughput_per_min=10.0, capacity_per_min=60.0, active_tracks=5, density_saturation=0
        )
        assert level is CongestionLevel.FREE_FLOW


class TestThroughputWindow:
    def test_records_within_the_window_are_counted(self) -> None:
        window = ThroughputWindow(window_seconds=60.0)
        t0 = datetime(2026, 1, 1, tzinfo=UTC)

        window.record(5, now=t0)

        assert window.per_minute(now=t0) == pytest.approx(5.0)

    def test_events_expire_after_the_window_elapses(self) -> None:
        window = ThroughputWindow(window_seconds=60.0)
        t0 = datetime(2026, 1, 1, tzinfo=UTC)

        window.record(5, now=t0)

        assert window.per_minute(now=t0 + timedelta(seconds=70)) == 0.0

    def test_partial_expiry_keeps_events_still_inside_the_window(self) -> None:
        window = ThroughputWindow(window_seconds=60.0)
        t0 = datetime(2026, 1, 1, tzinfo=UTC)

        window.record(2, now=t0)
        window.record(3, now=t0 + timedelta(seconds=30))

        # At t0+65s: the first batch (age 65s) has expired; the second (age 35s) has not.
        checked_at = t0 + timedelta(seconds=65)
        assert window.per_minute(now=checked_at) == pytest.approx(3.0)

    def test_defaults_to_real_time_when_now_is_not_supplied(self) -> None:
        window = ThroughputWindow(window_seconds=60.0)
        window.record(1)
        assert window.per_minute() == pytest.approx(1.0)
