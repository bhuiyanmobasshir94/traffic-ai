"""Adapters from `Database` to the two consumers of `CrossingRepository`.

The repository wants an `AsyncSession` that its caller owns; the writer and the API
routes want something they can call without thinking about sessions. Both adapters
open ONE session per call and close it on the way out, so no session is ever held
across a flush interval or across requests — a pooled connection is checked out
only while a query is actually running.

`RepositorySink` is the write side (it satisfies the writer's `_EventSink`).
`RepositoryHistory` is the read side, and is the only thing the API layer touches:
it keeps SQLAlchemy sessions and rows out of the routes, which stay ignorant of
the ORM.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from traffic_ai.db.repository import CrossingRepository
from traffic_ai.db.session import Database
from traffic_ai.domain import CrossingEvent


class RepositorySink:
    """Write adapter: `CrossingWriter` flushes a batch, this commits it."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def add_many(self, events: Sequence[CrossingEvent]) -> int:
        async with self._database.session() as session:
            return await CrossingRepository(session).add_many(events)


class RepositoryHistory:
    """Read adapter behind the `/api/history/*` routes."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def recent(self, camera_id: str | None, limit: int) -> list[CrossingEvent]:
        async with self._database.session() as session:
            return await CrossingRepository(session).recent(camera_id, limit)

    async def counts_by_class(
        self, camera_id: str | None, since: datetime, until: datetime
    ) -> dict[str, dict[str, int]]:
        async with self._database.session() as session:
            return await CrossingRepository(session).counts_by_class(camera_id, since, until)

    async def hourly_totals(
        self, camera_id: str | None, since: datetime, until: datetime
    ) -> list[tuple[datetime, int]]:
        async with self._database.session() as session:
            return await CrossingRepository(session).hourly_totals(camera_id, since, until)
