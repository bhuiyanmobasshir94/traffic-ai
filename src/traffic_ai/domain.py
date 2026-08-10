"""The contract between the worker and the UI.

These models are the only thing the two services agree on. The worker writes them
into Redis and serves them over HTTP; the UI reads them and renders. Changing a
field here is an API change — both sides must move together.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# COCO classes the detector is allowed to report. Anything else is discarded at
# the pipeline boundary rather than leaking an unexpected label into the UI.
VEHICLE_CLASSES: tuple[str, ...] = ("car", "motorcycle", "bus", "truck", "bicycle")


class Direction(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"


class CongestionLevel(StrEnum):
    FREE_FLOW = "free_flow"
    MODERATE = "moderate"
    HEAVY = "heavy"
    STANDSTILL = "standstill"

    @property
    def map_color(self) -> str:
        return {
            CongestionLevel.FREE_FLOW: "green",
            CongestionLevel.MODERATE: "orange",
            CongestionLevel.HEAVY: "red",
            CongestionLevel.STANDSTILL: "darkred",
        }[self]

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


class PipelineStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    STALLED = "stalled"
    ERROR = "error"
    STOPPED = "stopped"


class ClassCounts(BaseModel):
    """Vehicles counted crossing the line, by class, since the pipeline started."""

    car: int = 0
    motorcycle: int = 0
    bus: int = 0
    truck: int = 0
    bicycle: int = 0

    @property
    def total(self) -> int:
        return self.car + self.motorcycle + self.bus + self.truck + self.bicycle

    def increment(self, vehicle_class: str) -> None:
        if hasattr(self, vehicle_class):
            setattr(self, vehicle_class, getattr(self, vehicle_class) + 1)


class CrossingEvent(BaseModel):
    """One vehicle crossing the counting line. Append-only; never revised."""

    camera_id: str
    track_id: int
    vehicle_class: str
    direction: Direction
    crossed_at: datetime
    confidence: float = Field(ge=0.0, le=1.0)

    # Populated only when a plate model is configured. `None` means "not read",
    # never "unreadable" and never a placeholder.
    plate_text: str | None = None
    plate_confidence: float | None = None


class CameraState(BaseModel):
    """Everything the UI needs to render one camera. Written every pipeline tick."""

    camera_id: str
    name: str
    status: PipelineStatus
    updated_at: datetime

    counts: dict[Direction, ClassCounts] = Field(
        default_factory=lambda: {d: ClassCounts() for d in Direction}
    )
    active_tracks: int = 0
    throughput_per_min: float = 0.0
    congestion: CongestionLevel = CongestionLevel.FREE_FLOW
    pipeline_fps: float = 0.0
    frames_processed: int = 0

    anpr_enabled: bool = False
    error: str | None = None

    @property
    def total_counted(self) -> int:
        return sum(c.total for c in self.counts.values())

    def age_seconds(self, *, now: datetime | None = None) -> float:
        reference = now or datetime.now(UTC)
        return max(0.0, (reference - self.updated_at).total_seconds())

    def is_stale(self, ttl_seconds: float) -> bool:
        """True when this state is too old to present as live."""
        return self.age_seconds() > ttl_seconds


class CameraSummary(BaseModel):
    """Static description of a camera. Does not change at runtime."""

    camera_id: str
    name: str
    latitude: float
    longitude: float
    description: str = ""


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    ready: bool
    redis: bool
    cameras_running: int
    cameras_total: int
    detail: str | None = None
