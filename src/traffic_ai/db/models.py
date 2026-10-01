"""Table definitions for crossing history.

`direction`, `vehicle_class`, and `congestion` are stored as short strings rather
than native Postgres enums. `VEHICLE_CLASSES` is expected to change, and altering
an enum type is a heavier migration than the integrity it buys — the pipeline
already filters to the allowlist before anything reaches this layer.

Every timestamp is timezone-aware. A naive datetime stored here would be read back
as whatever the server's zone happened to be, which silently shifts history.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared metadata, so Alembic autogenerate sees every table in one place."""


class CrossingEventRow(Base):
    """One vehicle crossing the counting line. Append-only, mirroring `CrossingEvent`."""

    __tablename__ = "crossing_events"
    __table_args__ = (
        # Every history query is "this camera, newest first", so the index is
        # shaped to serve that scan without a separate sort.
        Index("ix_crossing_events_camera_crossed_at", "camera_id", text("crossed_at DESC")),
        # Cross-camera time-range queries have no camera_id to lead with.
        Index("ix_crossing_events_crossed_at", "crossed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False)
    track_id: Mapped[int] = mapped_column(Integer, nullable=False)
    vehicle_class: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    crossed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    # NULL means "not read" — never unreadable, never a placeholder. No plate model
    # ships with this project, so in practice these stay NULL until one is supplied.
    plate_text: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plate_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Server-side so the row records when the database accepted it, which is what
    # distinguishes a delayed flush from a late crossing when debugging gaps.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CameraCountSnapshotRow(Base):
    """Periodic rollup of live counts, so trends never scan every crossing event."""

    __tablename__ = "camera_count_snapshots"
    __table_args__ = (
        # A retried snapshot write lands on the same bucket and updates it in place
        # (see `CrossingRepository.upsert_snapshots`) instead of double-counting.
        UniqueConstraint(
            "camera_id",
            "captured_at",
            "direction",
            "vehicle_class",
            name="uq_camera_count_snapshots_bucket",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    vehicle_class: Mapped[str] = mapped_column(String(32), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    congestion: Mapped[str] = mapped_column(String(16), nullable=False)
    throughput_per_min: Mapped[float] = mapped_column(Float, nullable=False)
