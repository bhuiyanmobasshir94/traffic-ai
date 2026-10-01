"""Reads and writes for crossing history.

Everything that leaves this module is a Pydantic model or a plain value, never an
ORM row. The API layer must not depend on SQLAlchemy, and a row escaping its
session would raise `DetachedInstanceError` the first time something touched it.

Aggregation (`counts_by_class`, `hourly_totals`) is done in SQL with GROUP BY.
Pulling rows into Python to count them would move the whole history across the
wire to produce a handful of numbers.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import Select, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from traffic_ai.db.models import CameraCountSnapshotRow, CrossingEventRow
from traffic_ai.domain import CongestionLevel, CrossingEvent, Direction


class CameraCountSnapshot(BaseModel):
    """One rollup bucket: a class's running count in one direction at one camera.

    Lives here rather than in `domain.py` because nothing outside the persistence
    layer produces or consumes it yet; promoting it is a contract change that
    belongs with whoever wires the snapshotter.
    """

    camera_id: str
    captured_at: datetime
    direction: Direction
    vehicle_class: str
    count: int
    congestion: CongestionLevel
    throughput_per_min: float


def _to_row(event: CrossingEvent) -> CrossingEventRow:
    return CrossingEventRow(
        camera_id=event.camera_id,
        track_id=event.track_id,
        vehicle_class=event.vehicle_class,
        direction=event.direction.value,
        crossed_at=event.crossed_at,
        confidence=event.confidence,
        plate_text=event.plate_text,
        plate_confidence=event.plate_confidence,
    )


def _to_event(row: CrossingEventRow) -> CrossingEvent:
    return CrossingEvent(
        camera_id=row.camera_id,
        track_id=row.track_id,
        vehicle_class=row.vehicle_class,
        direction=Direction(row.direction),
        crossed_at=row.crossed_at,
        confidence=row.confidence,
        plate_text=row.plate_text,
        plate_confidence=row.plate_confidence,
    )


def _in_window(stmt: Select, camera_id: str | None, since: datetime, until: datetime) -> Select:
    """Half-open `[since, until)`, so adjacent windows never count a crossing twice."""
    stmt = stmt.where(CrossingEventRow.crossed_at >= since, CrossingEventRow.crossed_at < until)
    if camera_id is not None:
        stmt = stmt.where(CrossingEventRow.camera_id == camera_id)
    return stmt


class CrossingRepository:
    """Query and write surface over one `AsyncSession`.

    Write methods commit: a flush is the unit of work, and the writer treats one
    call as one all-or-nothing batch. The caller owns the session's lifetime.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_many(self, events: Sequence[CrossingEvent]) -> int:
        """Insert crossings in one transaction. Returns the number of rows written."""
        if not events:
            return 0
        self._session.add_all([_to_row(e) for e in events])
        await self._session.commit()
        return len(events)

    async def recent(self, camera_id: str | None, limit: int) -> list[CrossingEvent]:
        """Newest-first. `camera_id=None` merges across cameras."""
        stmt = select(CrossingEventRow).order_by(CrossingEventRow.crossed_at.desc()).limit(limit)
        if camera_id is not None:
            stmt = stmt.where(CrossingEventRow.camera_id == camera_id)
        result = await self._session.execute(stmt)
        return [_to_event(row) for row in result.scalars()]

    async def counts_by_class(
        self, camera_id: str | None, since: datetime, until: datetime
    ) -> dict[str, dict[str, int]]:
        """Crossings in `[since, until)`, keyed direction -> vehicle_class -> count."""
        stmt = _in_window(
            select(
                CrossingEventRow.direction,
                CrossingEventRow.vehicle_class,
                func.count().label("n"),
            ),
            camera_id,
            since,
            until,
        ).group_by(CrossingEventRow.direction, CrossingEventRow.vehicle_class)

        out: dict[str, dict[str, int]] = {}
        for direction, vehicle_class, n in (await self._session.execute(stmt)).all():
            out.setdefault(direction, {})[vehicle_class] = n
        return out

    async def hourly_totals(
        self, camera_id: str | None, since: datetime, until: datetime
    ) -> list[tuple[datetime, int]]:
        """Crossings per hour in `[since, until)`, oldest hour first.

        Hours with no crossings are absent rather than zero-filled: a gap here
        could equally be a quiet road or a worker that was down, and this layer
        cannot tell which. Callers that chart it decide how to render a gap.
        """
        # Literal arguments, not bind parameters: with parameters, Postgres sees the
        # SELECT and GROUP BY copies as different expressions and rejects the query.
        # Truncating in UTC keeps hour boundaries independent of the server's
        # configured TimeZone.
        bucket = func.date_trunc(
            literal_column("'hour'"), CrossingEventRow.crossed_at, literal_column("'UTC'")
        ).label("bucket")
        stmt = (
            _in_window(select(bucket, func.count().label("n")), camera_id, since, until)
            .group_by(bucket)
            .order_by(bucket)
        )
        return [(hour, n) for hour, n in (await self._session.execute(stmt)).all()]

    async def upsert_snapshots(self, rows: Sequence[CameraCountSnapshot]) -> int:
        """Idempotent against the bucket's unique constraint. Returns rows submitted."""
        if not rows:
            return 0
        stmt = pg_insert(CameraCountSnapshotRow).values(
            [
                {
                    "camera_id": r.camera_id,
                    "captured_at": r.captured_at,
                    "direction": r.direction.value,
                    "vehicle_class": r.vehicle_class,
                    "count": r.count,
                    "congestion": r.congestion.value,
                    "throughput_per_min": r.throughput_per_min,
                }
                for r in rows
            ]
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_camera_count_snapshots_bucket",
            # The latest write wins: a retry carries the same or fresher numbers.
            set_={
                "count": stmt.excluded.count,
                "congestion": stmt.excluded.congestion,
                "throughput_per_min": stmt.excluded.throughput_per_min,
            },
        )
        await self._session.execute(stmt)
        await self._session.commit()
        return len(rows)
