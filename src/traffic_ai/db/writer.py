"""Background batching writer: crossings go to Postgres without touching the frame loop.

One INSERT per vehicle would put a network round-trip inside the pipeline tick, so
`submit()` only appends to memory and a separate `run()` task flushes in batches.

The governing rule is that a database failure must never reach the pipeline. A
flush that raises is logged and its batch is abandoned — history is lost, the live
dashboard is not. The batch is deliberately not re-queued: retrying through a long
outage would only grow the buffer toward its cap and then evict newer events in
favour of older ones.

Losing history is acceptable; losing it SILENTLY is not. Every event that will never
reach the database is counted (`lost_count`, and the `history_events_lost_total`
metric by reason) so the shortfall is visible to an operator and to the readiness
endpoint rather than showing up later as totals that are quietly too low.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Sequence
from typing import Protocol

from traffic_ai import metrics
from traffic_ai.config import Settings
from traffic_ai.db.errors import describe_db_error
from traffic_ai.domain import CrossingEvent
from traffic_ai.logging import get_logger

log = get_logger(__name__)

# A fixed ceiling rather than a Settings field: it is a safety bound against an
# unbounded buffer during an outage, not an operational knob. At the default flush
# settings this is minutes of traffic across both cameras.
DEFAULT_BUFFER_LIMIT = 10_000


class _EventSink(Protocol):
    """The slice of `CrossingRepository` the writer depends on.

    Structural, so the writer is testable against a stub with no database and no
    SQLAlchemy session in sight.
    """

    async def add_many(self, events: Sequence[CrossingEvent]) -> int: ...


class CrossingWriter:
    """Buffers `CrossingEvent`s and flushes them in batches from a background task."""

    def __init__(
        self,
        sink: _EventSink,
        *,
        flush_interval_seconds: float,
        flush_max_batch: int,
        buffer_limit: int = DEFAULT_BUFFER_LIMIT,
    ) -> None:
        self._sink = sink
        self._flush_interval = flush_interval_seconds
        self._flush_max_batch = flush_max_batch
        self._buffer_limit = buffer_limit
        self._buffer: deque[CrossingEvent] = deque()
        self._dropped = 0
        self._lost = 0
        self._stop_requested = False
        # Wakes the run loop early when a batch fills, so a burst is not held
        # back until the next interval tick.
        self._wake = asyncio.Event()

    @classmethod
    def from_settings(cls, sink: _EventSink, settings: Settings) -> CrossingWriter:
        return cls(
            sink,
            flush_interval_seconds=settings.db_flush_interval_seconds,
            flush_max_batch=settings.db_flush_max_batch,
        )

    @property
    def dropped_count(self) -> int:
        """Events evicted because the buffer was full — i.e. history lost to an outage."""
        return self._dropped

    @property
    def lost_count(self) -> int:
        """Every event that will never reach history, whatever the reason.

        The sum of buffer evictions (`dropped_count`) and events in batches the
        database refused. Cumulative since the worker started; it resets with the
        process, like the live counters.
        """
        return self._lost

    @property
    def pending_count(self) -> int:
        return len(self._buffer)

    def _record_loss(self, count: int, reason: str) -> None:
        self._lost += count
        metrics.history_events_lost_total.labels(reason).inc(count)

    def submit(self, event: CrossingEvent) -> None:
        """Non-blocking and synchronous, so the pipeline's hot loop can call it freely.

        When the buffer is full the oldest event is evicted: during an outage the
        newest crossings are the ones worth keeping.
        """
        if len(self._buffer) >= self._buffer_limit:
            self._buffer.popleft()
            self._dropped += 1
            self._record_loss(1, metrics.HISTORY_LOSS_BUFFER_FULL)
            log.warning(
                "db.buffer_full_dropped_oldest",
                dropped_total=self._dropped,
                buffer_limit=self._buffer_limit,
            )
        self._buffer.append(event)
        if len(self._buffer) >= self._flush_max_batch:
            self._wake.set()

    def request_stop(self) -> None:
        self._stop_requested = True
        # Without this the loop would sleep out the rest of its interval before
        # noticing the stop, delaying shutdown by up to `flush_interval_seconds`.
        self._wake.set()

    async def run(self) -> None:
        """Flushes until `request_stop()`, then drains what is left. Never raises out."""
        while not self._stop_requested:
            # A timeout is the normal interval tick, not an error.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._flush_interval)
            self._wake.clear()
            await self._flush_one_batch()
            # A burst can leave more than one batch behind; re-arm so the rest is
            # not held until the next interval tick.
            if len(self._buffer) >= self._flush_max_batch:
                self._wake.set()

        # Drain in batch-sized chunks: a stop must not discard buffered crossings,
        # and one oversized INSERT after a long outage is exactly what batching avoids.
        while self._buffer:
            await self._flush_one_batch()

    async def _flush_one_batch(self) -> None:
        if not self._buffer:
            return
        batch = [
            self._buffer.popleft() for _ in range(min(len(self._buffer), self._flush_max_batch))
        ]
        try:
            await self._sink.add_many(batch)
            return
        except Exception as exc:
            # Losing history is acceptable; taking the live dashboard down is not.
            # This is the one place a broad catch is correct — any driver, pool, or
            # network error must stop at this boundary.
            failure = exc

        lost = len(batch)
        if len(batch) > 1:
            # One row the database rejects fails the whole INSERT, and with it every
            # good row beside it. Try each half once before giving up on the batch, so
            # a poison row costs at most half a batch instead of all of it. The INSERT
            # is a single transaction, so the failed attempt wrote nothing and a retry
            # cannot duplicate rows.
            #
            # Deliberately one split and no deeper. During an outage every attempt is a
            # failing round trip, possibly a full pool timeout: bisecting all the way
            # down would multiply that by the batch size for every batch, while one
            # split costs two extra attempts.
            middle = len(batch) // 2
            lost = 0
            for half in (batch[:middle], batch[middle:]):
                try:
                    await self._sink.add_many(half)
                except Exception:
                    # Counted and logged below, with the first failure's cause. A second
                    # cause for the same batch adds nothing an operator can act on.
                    lost += len(half)
        if lost:
            self._record_loss(lost, metrics.HISTORY_LOSS_FLUSH_FAILED)
        log.warning(
            "db.flush_failed",
            batch_size=len(batch),
            lost=lost,
            **describe_db_error(failure),
        )
