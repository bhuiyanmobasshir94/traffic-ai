"""Camera and road-network registry.

The map geometry is carried over verbatim from the original demo so the dashboard
keeps its identity — same Dhaka corridors, same two toll plazas. The coordinates
are stored here as [latitude, longitude], which is what Folium expects; the
original code stored them reversed and swapped them at import with a `map(lambda)`
that had to be repeated in all three files.

Congestion colours are no longer hardcoded. Each road segment names the camera it
derives from, and takes that camera's live congestion level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Road corridors (latitude, longitude)
# --------------------------------------------------------------------------

DHAKA_BYPASS_EAST: list[list[float]] = [
    [23.825473, 90.422716],
    [23.826572, 90.428467],
    [23.827907, 90.433874],
    [23.828770, 90.439625],
    [23.829791, 90.444174],
    [23.830733, 90.449066],
    [23.831518, 90.453014],
    [23.832618, 90.457220],
    [23.833795, 90.463057],
    [23.835444, 90.468636],
    [23.836386, 90.474043],
    [23.837171, 90.479794],
    [23.837721, 90.486832],
    [23.837250, 90.496616],
    [23.837328, 90.506487],
    [23.837093, 90.511551],
    [23.837407, 90.518847],
    [23.837093, 90.523310],
    [23.834659, 90.526915],
    [23.834423, 90.529060],
    [23.834659, 90.533438],
    [23.834502, 90.538845],
    [23.834502, 90.540390],
    [23.835051, 90.542793],
    [23.836465, 90.545969],
    [23.837093, 90.548973],
]

NORTH_APPROACH: list[list[float]] = [
    [23.937938, 90.385551],
    [23.926641, 90.390015],
    [23.914401, 90.395508],
    [23.902788, 90.398598],
    [23.892744, 90.400314],
    [23.883012, 90.400658],
    [23.874222, 90.400658],
    [23.862606, 90.400658],
    [23.850674, 90.408211],
    [23.839055, 90.419540],
    [23.826179, 90.421600],
]

SOUTH_APPROACH: list[list[float]] = [
    [23.825237, 90.422974],
    [23.820840, 90.419884],
    [23.816757, 90.410957],
    [23.812046, 90.404778],
    [23.803879, 90.402031],
    [23.792884, 90.401001],
    [23.780946, 90.398941],
    [23.770578, 90.395851],
    [23.762723, 90.394821],
    [23.759267, 90.394478],
]

MAP_CENTER: list[float] = [23.828725716729313, 90.44034752339583]
MAP_ZOOM: int = 13


# --------------------------------------------------------------------------
# Cameras
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CountingLine:
    """A virtual gate across the frame, in normalized coordinates.

    Normalized (0.0-1.0) rather than pixels so the line survives a change of
    `frame_width` or a different-resolution source video. `start` and `end` are
    (x, y); a track is counted when its anchor point crosses the segment.

    Which side is "incoming" is a property of the camera's viewpoint, not of the
    geometry, so it is declared rather than inferred.
    """

    start: tuple[float, float] = (0.02, 0.55)
    end: tuple[float, float] = (0.98, 0.55)
    # When True, a track crossing from the low-y side to the high-y side counts
    # as INCOMING. Flip for a camera mounted facing the opposite way.
    incoming_is_downward: bool = True


@dataclass(frozen=True)
class CameraConfig:
    camera_id: str
    name: str
    video_filename: str
    latitude: float
    longitude: float
    description: str = ""
    counting_line: CountingLine = field(default_factory=CountingLine)
    # Vehicles per minute at which the corridor is considered saturated. Used to
    # derive a congestion level from measured throughput instead of hardcoding it.
    capacity_per_min: float = 60.0

    def video_path(self, video_dir: Path) -> Path:
        return video_dir / self.video_filename


CAMERAS: tuple[CameraConfig, ...] = (
    CameraConfig(
        camera_id="toll-plaza-a",
        name="Toll Plaza A",
        video_filename="toll-plaza-a.mp4",
        latitude=23.828725716729313,
        longitude=90.44034752339583,
        description="Dhaka Bypass eastbound, main toll gate.",
        capacity_per_min=70.0,
    ),
    CameraConfig(
        camera_id="toll-plaza-b",
        name="Toll Plaza B",
        video_filename="toll-plaza-b.mp4",
        latitude=23.836842165760064,
        longitude=90.47714944282039,
        description="Dhaka Bypass eastbound, secondary gate.",
        capacity_per_min=50.0,
    ),
)

CAMERAS_BY_ID: dict[str, CameraConfig] = {c.camera_id: c for c in CAMERAS}


@dataclass(frozen=True)
class RoadSegment:
    """A corridor drawn on the map, coloured by a camera's live congestion."""

    name: str
    path: list[list[float]]
    camera_id: str


ROAD_SEGMENTS: tuple[RoadSegment, ...] = (
    RoadSegment("Dhaka Bypass (east)", DHAKA_BYPASS_EAST, "toll-plaza-a"),
    RoadSegment("North approach", NORTH_APPROACH, "toll-plaza-b"),
    RoadSegment("South approach", SOUTH_APPROACH, "toll-plaza-a"),
)


def get_camera(camera_id: str) -> CameraConfig | None:
    """Look up a camera. Returns None for unknown ids — never raises.

    Callers receive camera ids from map clicks and URL parameters, so this is an
    allowlist boundary: anything not in the registry does not exist.
    """
    return CAMERAS_BY_ID.get(camera_id)
